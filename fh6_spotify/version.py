"""Single source of truth for Segue\'s version string.\n\nBump this on every release, then run scripts/release.py to push the\nmatching latest.json to the public segue-releases repo. The installer\n(installer/Segue.iss) keeps its own MyAppVersion constant - they must\nmatch. The release script checks both before allowing a push.\n"""

VERSION = "1.4.6"
VERSION_LABEL = ""
NO_CAROUSEL = True
KEYBOARD_SUMMON = False
