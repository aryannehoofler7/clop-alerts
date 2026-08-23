# Notification icons

Drop resource sprites in here and toast alerts wear them: a buy order for copper arrives under the
copper sprite, a new report under `report.png`. The folder is selected in `../settings.json` with:

```json
"notifications": { "icon_dir": "icons" }
```

Set `icon_dir` to `null` for no icons at all, in which case toasts show the icon of whichever
application `notifications.app_id` names.

## Naming

A file is matched by its name without the extension, ignoring case. For a **market alert** the
monitor tries three names in order, so a sprite pack can keep whichever naming it arrived with:

| Tried | Example for Copper |
| --- | --- |
| The game's own name (`resourcedefs.name`) | `Copper.png` |
| The short `Dashboard-Stockpile` label | `M Parts.png` (for Machinery Parts) |
| The numeric `resource_id` | `2.png` |

`goods.py` is the full table of all 31 goods under each of those names.

Every other alert looks for one name: `messages.png`, `news.png`, `report.png`, or `4chan.png`.

Anything that matched nothing falls back to `default.png`, and an alert with no `default.png` to
fall back on simply arrives without an icon.

## Formats

`.png`, `.jpg`, `.jpeg` and `.gif` render inside a toast. `.ico` does **not** — Windows silently
drops the image rather than reporting it, so convert those to PNG first.

Square images look best: they are shown uncropped in a small square beside the text. Windows
scales them down, so roughly 64x64 or larger is enough.

Confirm the redistribution rights for third-party sprites before publishing this repository.
