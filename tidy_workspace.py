"""One-shot cleanup: move generated files that older runs left in the
workspace root into the organized folders new runs use, so the folder
holds only the tools, your data, and your settings.

Moves (never deletes — everything stays findable):
    catalog_*.md         -> catalogs/
    research_pack_*.md   -> research_packs/
    question_pack_*.md   -> answers/
    answer_*.md          -> answers/

Nothing else is touched. Shows the plan and asks before moving;
--yes skips the confirmation (used by the desktop app).

Usage:
    python tidy_workspace.py
    python tidy_workspace.py --yes
"""
import argparse
import glob
import os
import shutil

MOVE_RULES = [
    ("catalog_*.md", "catalogs"),
    ("research_pack_*.md", "research_packs"),
    ("question_pack_*.md", "answers"),
    ("answer_*.md", "answers"),
]


def main():
    parser = argparse.ArgumentParser(description="Move stray generated files into their organized folders.")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    plan = []
    for pattern, dest_dir in MOVE_RULES:
        for path in sorted(glob.glob(pattern)):
            if os.path.isfile(path):
                plan.append((path, dest_dir))

    if not plan:
        print("Nothing to tidy — the workspace root has no stray generated files.")
        return

    print(f"{len(plan)} file(s) to move:")
    for path, dest_dir in plan:
        print(f"  {path}  ->  {dest_dir}/")
    if not args.yes and input("\nMove them? [Y/n] ").strip().lower() == "n":
        print("Cancelled.")
        return

    moved = 0
    for path, dest_dir in plan:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(path))
        if os.path.exists(dest):
            base, ext = os.path.splitext(os.path.basename(path))
            dest = os.path.join(dest_dir, f"{base}_moved{ext}")
        shutil.move(path, dest)
        moved += 1

    print(f"Done — {moved} file(s) moved. The workspace root now holds only tools, data, and settings.")


if __name__ == "__main__":
    main()
