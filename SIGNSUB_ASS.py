"""Standalone CLI: extract sign subs from a full .ass subtitle file.

This complements ``SIGNSUB.py`` (which works on .mkv files). It skips FFmpeg
entirely and filters a full ``.ass`` file down to its sign/typesetting/SFX
events, writing the result next to the input as ``{name}_signs.ass``.

The dialogue styles to drop are auto-detected per file: fansub releases that
use ``Default``/``Song`` (e.g. ``Fullsub.ass``) drop those, while SubsPlus+
releases that use ``Subtitle``/``Subtitle-Alt`` (e.g. ``NEW FULL SUB.ass``)
drop those instead and keep the positioned ``Caption`` signs.

If the input carries embedded attachments (``[Fonts]`` / ``[Graphics]``
sections, as produced when extracting subtitles from an MKV), they are all
decoded and copied into a ``{name}_attachments`` folder next to the output.

Usage:
    python SIGNSUB_ASS.py            # prompts for the .ass path
    python SIGNSUB_ASS.py full.ass   # direct path argument
"""

import sys
from pathlib import Path

from signsub.processing.pipeline import filter_ass_file_auto


def extract_signs_from_ass(ass_path):
    """Filters a full .ass file, leaving only signs/SFX. Returns the output path."""
    input_ass = Path(ass_path)
    output_ass = input_ass.with_name(f"{input_ass.stem}_signs.ass")

    print("Filtering out dialogue tracks to leave only signs/SFX...")
    kept, dropped, banned, attachments = filter_ass_file_auto(
        input_ass, output_ass, with_attachments=True
    )
    print(f"Detected dialogue style(s): {', '.join(sorted(banned))}")
    print(f"Kept {kept} sign/SFX event(s), dropped {dropped} dialogue event(s).")
    if attachments:
        print(f"Copied {len(attachments)} attachment(s):")
        for attachment in attachments:
            print(f"  📎 {attachment}")

    if kept == 0:
        print("No sign/typesetting events remained after filtering out dialogue.")
        try:
            output_ass.unlink()
        except OSError:
            pass
        return None
    return output_ass


def main():
    print("======================================================")
    print("  Full ASS Sign Extractor   ")
    print("======================================================\n")

    if len(sys.argv) > 1:
        ass_path = sys.argv[1]
    else:
        ass_path = input("Drag & drop your .ass file here and press Enter:\n").strip()
    ass_path = ass_path.strip('"').strip("'")

    if not ass_path.lower().endswith((".ass", ".ssa")):
        print(f"❌ Error: The file '{ass_path}' is not an .ass/.ssa subtitle file.")
        return

    if not Path(ass_path).is_file():
        print(f"❌ Error: The file path '{ass_path}' does not exist.")
        return

    output_ass = extract_signs_from_ass(ass_path)

    if output_ass:
        print(f"\n🎉 Process Complete!")
        print(f"👉 Generated signs-only file: {output_ass}")


if __name__ == "__main__":
    main()
