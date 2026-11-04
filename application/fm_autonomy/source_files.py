"""Walk owned release sources without descending into toolchains or run artifacts."""
import os
from pathlib import Path
EXCLUDED={'dist','runs','private','__pycache__','upstream_evidence','reference_assets_3361',
          'compilation339','cross339','build','build-cross','handoff'}
def walk_files(root):
    for folder,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if d not in EXCLUDED and not d.startswith('.') and not (Path(folder)/d).is_symlink()]
        for name in files:
            path=Path(folder)/name
            if not path.is_symlink():yield path
