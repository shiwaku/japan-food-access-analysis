#!/usr/bin/env python3
"""HTTP server with Range request support (required for PMTiles).

ローカル開発専用の追加エンドポイント:
  /dev/stores.geojson  店舗レイヤ（FOOD_STORES の parquet）を GeoJSON で返す。
                       ビューワは localhost のときだけこれを読んで店舗を点で描く。
                       店舗レイヤは再配布可否が元 repo 依存なので公開側には出さない。
"""
import json, os, sys, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

FOOD_STORES = os.environ.get("FOOD_STORES", "input/food_store_master_public_noosm.parquet")

_stores_cache = {}
_stores_lock = threading.Lock()


def build_stores_geojson() -> bytes:
    """FOOD_STORES を GeoJSON(bytes) にする。初回だけ作ってメモリに置く。"""
    with _stores_lock:
        if "body" in _stores_cache:
            return _stores_cache["body"]
        import duckdb  # 遅延 import（店舗を見ないときは不要）
        path = FOOD_STORES.replace("\\", "/")
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        rows = duckdb.sql(f"""
            select lng, lat, cat,
                   coalesce(name, '')  as name,
                   coalesce(brand, '') as brand
            from '{path}'
            where lat is not null and lng is not null
        """).fetchall()
        feats = [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lng, 6), round(lat, 6)]},
            "properties": {"cat": cat, "name": name, "brand": brand},
        } for lng, lat, cat, name, brand in rows]
        body = json.dumps({"type": "FeatureCollection", "features": feats},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _stores_cache["body"] = body
        print(f"[dev] stores.geojson: {len(feats):,} 店 / {len(body)/1e6:.1f} MB ({path})")
        return body


class RangeHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] == "/dev/stores.geojson":
            return self.serve_stores()
        return super().do_GET()

    def serve_stores(self):
        try:
            body = build_stores_geojson()
        except FileNotFoundError as e:
            self.send_error(404, f"FOOD_STORES not found: {e}")
            return
        except Exception as e:  # duckdb 未導入など
            self.send_error(500, f"stores.geojson: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/geo+json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()

        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        fs = os.fstat(f.fileno())
        file_size = fs.st_size
        ctype = self.guess_type(path)
        range_header = self.headers.get("Range")

        if range_header:
            try:
                unit, rng = range_header.strip().split("=", 1)
                start_s, end_s = rng.split("-", 1)
                start = int(start_s) if start_s else 0
                end   = int(end_s)   if end_s   else file_size - 1
                end   = min(end, file_size - 1)
                length = end - start + 1
                f.seek(start)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
                self.end_headers()
                return f
            except Exception as e:
                f.close()
                self.send_error(400, f"Bad Range header: {e}")
                return None
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
            self.end_headers()
            return f

    def log_message(self, fmt, *args):
        # 省略表示（Range リクエストが大量に出るため）
        if args and str(args[1]) not in ("200", "206"):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"Serving at http://localhost:{port}/docs/")
    print(f"  店舗レイヤ（ローカル専用）: /dev/stores.geojson ← {FOOD_STORES}")
    # **ThreadingHTTPServer でないと使い物にならない。** PMTiles は並列に大量の Range
    # リクエストを投げるうえ、ブラウザが keep-alive で接続を掴んだままにするので、
    # シングルスレッドの HTTPServer だと後続のリクエストが全部ブロックされる。
    with ThreadingHTTPServer(("", port), RangeHandler) as httpd:
        httpd.serve_forever()
