"""One-step runner for the whole pipeline: Stage 1 resolves DOIs and
grabs every freely available copy (Unpaywall), then Stage 2 opens the
browser and fetches the rest through the university proxy — only if
anything is actually left. Run this instead of the two scripts
separately:

    python download_papers.py

doi_resolver.py and proxy_download.py remain runnable on their own.
"""
import doi_resolver
import proxy_download


def main():
    print("=" * 60)
    print("Stage 1: open-access downloads (no login needed)")
    print("=" * 60)
    doi_resolver.main()

    print()
    print("=" * 60)
    print("Stage 2: university proxy for whatever's left")
    print("=" * 60)
    # Exits immediately with a friendly message if Stage 1 got everything.
    proxy_download.main()


if __name__ == "__main__":
    main()
