# Design-document figures

One sequence diagram per assessed module, rendered to **equal-size** PNGs
(2000×1440) for the design document
(`docs/<branch>/architecture_report_<branch>_full.md`):

| File | Module | Flow |
|---|---|---|
| `change_detection_sequence.png` | `change_detection.py` | the `classify()` change-detection decision |
| `single_file_transfer_sequence.png` | `single_file_transfer.py` | one file's chunk → dedup → commit transfer |

The `.mmd` files are the Mermaid sources; the writeup embeds the same
Mermaid inline, so the PNGs and the live diagrams stay in step.

Regenerate (both come out at identical pixel dimensions - the canvas is
fixed, each diagram is scaled to fit and centred):

```bash
npm install mermaid                 # provides mermaid.min.js
pip install playwright              # or use the demo's .venv
python figures/make_figures.py      # CHROMIUM=/path/to/chromium if needed
```
