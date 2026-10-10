"""Build the single-file Open WebUI photo tool from the package and the adapter.

Same reasoning as tools/build_owui_filter.py: Open WebUI tools are pasted in as
one file and most installs cannot pip install anything, so the part of the
package the tool needs is inlined. Here that is just photos.py — the providers,
the subject screening and the response parsing — so the provider code is
covered by the test suite instead of living only inside a pasted function.

    python tools/build_owui_photo.py          # writes dist/kitegate_openwebui_photo.py
    python tools/build_owui_photo.py --check  # fails if dist is out of date
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_owui_filter import module_body, splice  # noqa: E402

PKG = ROOT / "src" / "kitegate"
SOURCE = ROOT / "adapters" / "openwebui" / "kitegate_photo.py"
OUT = ROOT / "dist" / "kitegate_openwebui_photo.py"

# Only photos.py. The tool does not screen conversation, so it needs no engine,
# no packs and no signing: the policy tiers belong to the filter or the gateway.
MODULES = ("photos.py",)


def build() -> str:
    inlined = "\n".join(module_body(PKG / name) for name in MODULES)
    return splice(SOURCE.read_text(encoding="utf-8"), inlined,
                  "tools/build_owui_photo.py", "adapters/openwebui/kitegate_photo.py")


def main() -> None:
    built = build()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != built:
            raise SystemExit("dist/kitegate_openwebui_photo.py is out of date: "
                             "run tools/build_owui_photo.py")
        print("up to date")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(built, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(built.splitlines())} lines)")


if __name__ == "__main__":
    main()
