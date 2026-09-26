"""Mealie companion: post-import recipe processing (driven by Mealie's recipe_created event)
and the Mealie Toolkit page."""

__version__ = "0.1.0"

# Key written into a recipe's `extras` once it has been processed. Its presence is what stops
# a sweep from processing the same recipe twice. It keeps the service's original name on
# purpose: changing it would make every recipe processed so far look unprocessed.
MARKER_KEY = "mealie-hook"
