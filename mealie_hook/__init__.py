"""Post-import processing for Mealie, driven by Mealie's own recipe_created event."""

__version__ = "0.1.0"

# Key written into a recipe's `extras` once it has been processed. Its presence is what stops
# a sweep from processing the same recipe twice.
MARKER_KEY = "mealie-hook"
