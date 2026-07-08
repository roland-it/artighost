"""
Manual playbook embedding script.
Run by hand whenever support_playbooks/ has been regenerated and you want
the knowledge collection refreshed. Not automated/scheduled.
"""

import os
import glob
import sys

# so this script can import vectorstore.py — adjust if it's not in the same folder
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vectorstore

PLAYBOOK_DIR = "support_playbooks"


def chunk_markdown(text, max_chars=1500):
    sections = text.split("\n## ")
    chunks = []
    for i, s in enumerate(sections):
        s = s if i == 0 else "## " + s
        if len(s) > max_chars:
            for j in range(0, len(s), max_chars):
                chunks.append(s[j:j + max_chars])
        else:
            chunks.append(s)
    return [c.strip() for c in chunks if c.strip()]


def wipe_existing_playbook_entries():
    collection = vectorstore.get_collection("knowledge")
    if collection.count() == 0:
        return
    results = collection.get(include=["metadatas"])
    ids_to_delete = [
        id_ for id_, meta in zip(results["ids"], results["metadatas"])
        if meta.get("source") == "playbook"
    ]
    if ids_to_delete:
        collection.delete(ids=ids_to_delete)
        print(f"Deleted {len(ids_to_delete)} existing playbook entries.")


def main():
    files = glob.glob(os.path.join(PLAYBOOK_DIR, "*.md"))
    if not files:
        print(f"No .md files found in {PLAYBOOK_DIR}")
        return

    wipe_existing_playbook_entries()

    total_chunks = 0
    for filepath in files:
        bucket_name = os.path.splitext(os.path.basename(filepath))[0]
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()

        for chunk in chunk_markdown(text):
            vectorstore.add_knowledge(text=chunk, source="playbook", title=bucket_name)
            total_chunks += 1

    print(f"Embedded {total_chunks} chunks from {len(files)} playbook files.")


if __name__ == "__main__":
    main()
