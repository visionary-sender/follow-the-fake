"""Suite-wide settings: tests never read the VM's brand stop-list (HACK-66), so results do not depend on data."""

import os

os.environ["FTF_BRAND_STOPLIST"] = "off"
