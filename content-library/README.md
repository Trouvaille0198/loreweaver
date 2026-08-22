# Loreweaver external content library

This directory is a staging area for tabletop-game material that can be carried with a Loreweaver deployment.
It is intentionally **not** an installable `.lwpack`: source PDFs and external references must be reviewed and, when needed, converted into a Loreweaver module before import.

## Included files

`external/free-official/coc7/` contains two PDFs downloaded from Chaosium's official free-resource pages on 2026-08-22:

- `call-of-cthulhu-7e-quickstart-the-haunting.pdf` — Call of Cthulhu 7th Edition Quick-Start Rules, including **The Haunting**.
- `the-lightless-beacon.pdf` — the official introductory scenario **The Lightless Beacon**.

The SHA-256 values are recorded in `sources.json`.

## External references

`source_links` in `sources.json` records the D&D and CoC resources that were found but are not bundled here:

- OneShotsmith: a D&D 5e one-shot generator with Markdown export.
- D&D Adventure: a free HTML resource archive.
- Chaosium's **The Order of the Stone**: listed as a paid product, so it is not bundled.
- Mythos Japan scenarios: license and redistribution terms require checking on each product page.
- `coc_archive`: a reference archive whose README says that original copyrights remain with their authors.

## Import workflow

Loreweaver's module upload path accepts text-oriented sources. Verify the locally downloaded PDFs (SHA-256 against `sources.json`) and, when `pdftotext` (poppler-utils) is installed, convert them to text:

```console
./prepare-sources.sh --check-only          # verify integrity
./prepare-sources.sh --convert ./converted # verify + pdftotext into ./converted/<id>.txt
```

Then use the Keeper module-management surface to upload the text file and import it into the room. The module analyzer will extract scenes, NPCs, clues, timeline, threats, and keeper-only truths.

For a production `.lwpack`, create a reviewed Loreweaver card or Markdown module under a separate pack directory and build it with:

```console
uv run python -m app --pack path/to/pack
```

Do not publish or redistribute the bundled PDFs or converted module text unless the applicable publisher and author terms permit it. A free download is not automatically a redistribution license. For a public server, keep copyrighted source files private or fetch them only for an operator who is entitled to use them.
