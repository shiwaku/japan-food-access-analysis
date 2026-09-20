#!/bin/bash
tippecanoe \
  -Z 4 -z 13 \
  -l food_access \
  --no-tile-size-limit \
  --no-feature-limit \
  --coalesce-densest-as-needed \
  --force \
  -P \
  -o output/food_access_125m_v3.pmtiles \
  output/food_access_125m_v3.geojsonl
