# Plaque

A [Luvus](https://luvus.dev) module that publishes human titles onto **resumable** rows in the AGENTS sidebar.

Luvus does not parse native agent session stores for that list. Plaque reads them instead and pushes titles with `luvus ui agent-title push`. Live OSC titles still win. Luvus pane aliases (`=name`) are never sent as titles.

## What it reads

1. **Pi / Oh My Pi** — `session_info.name` (the source Luvus docs call out)
2. Other structured names on disk (OpenCode `session.title`, Grok `session_summary`, Claude summaries, …)
3. A short derived title from the session, when nothing structured exists

## Requirements

- Luvus **0.14.1** or newer (`ui agent-title`)
- `python3`
- In Luvus: **Settings → Layout → Show agent session title**

## Install

From GitHub:

```sh
luvus module install AsahiArt/Plaque
```

Local checkout:

```sh
luvus module link .
luvus module list
luvus module log asahiart.plaque
```

Refresh on demand:

```sh
luvus module run asahiart.plaque refresh
```

Titles repaint on startup and when resume rows change (`pane.created` / `closed` / `forked`, `pane.agent_status_changed`, `workspace.created`). There is no polling.

## Notes

- Empty titles are not pushed. `ui agent-title clear` drops every title this module owns.
- The CLI authenticates with the injected module token. Plaque does not pass, log, or persist it.
