These are the original Production batch files supplied for reference.

The pipeline does NOT execute them directly because they:
- prompt interactively for Version,
- process '*.las' in the current directory,
- create date/version folders.

Instead, scripts/02_build_eversource_points.py invokes las2las.exe directly
with the same CRS flags after the final point selection / clipping step:

  CT:   -epsg 6434 -survey_feet -elevation_survey_feet -vertical_navd88
  MA:   -epsg 6492 -survey_feet -elevation_survey_feet -vertical_navd88
  NH:   -epsg 6525 -survey_feet -elevation_survey_feet -vertical_navd88

The resulting LAS is then reopened and its header is validated.
