"""Render the requested E1 PDF and make numbered contact sheets for visual QA."""
import argparse
import hashlib
import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open(args.pdf)
    pages, text, outside = [], [], []
    for index, page in enumerate(document):
        target = args.output / f"page_{index + 1:02d}.png"
        page.get_pixmap(matrix=pymupdf.Matrix(1.25, 1.25), alpha=False).save(target)
        pages.append(target)
        text.append(page.get_text())
        for word in page.get_text("words"):
            if word[0] < -0.5 or word[1] < -0.5 or word[2] > page.rect.width + 0.5 or word[3] > page.rect.height + 0.5:
                outside.append({"page": index + 1, "word": word[4], "bbox": list(word[:4])})
    contacts = []
    for first in range(0, len(pages), 4):
        opened = [Image.open(path).convert("RGB") for path in pages[first:first + 4]]
        width = max(item.width for item in opened)
        height = max(item.height for item in opened)
        sheet = Image.new("RGB", (2 * width, 2 * (height + 26)), "#d8d8d8")
        draw = ImageDraw.Draw(sheet)
        for offset, item in enumerate(opened):
            x, y = (offset % 2) * width, (offset // 2) * (height + 26)
            draw.text((x + 10, y + 6), f"Page {first + offset + 1}", fill="black")
            sheet.paste(item, (x, y + 26))
        target = args.output / f"contact_{first // 4 + 1:02d}.png"
        sheet.save(target)
        contacts.append(str(target))
    (args.output / "extracted_text.txt").write_text("\n\n".join(text), encoding="utf-8")
    summary = {"pdf": str(args.pdf.resolve()), "sha256": hashlib.sha256(args.pdf.read_bytes()).hexdigest(),
               "pages": len(pages), "words_outside_page": outside, "contact_sheets": contacts,
               "visual_inspection_required": True}
    (args.output / "render_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
