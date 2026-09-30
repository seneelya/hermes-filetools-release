# hermes-filetools

A plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent): file tools with plain-text
answers, a context engine (manual and automatic compaction of a session into a distillate), and a
panel in the Hermes desktop app (prompt mirror, token strip, operations, config).

This repository holds RELEASES only — one commit and one tag (`vX.Y.Z`) per version. Development
happens elsewhere.

## Install

```
hermes plugins install https://github.com/seneelya/hermes-filetools-release
```

The plugin lands in `<HERMES_HOME>/plugins/hermes-filetools`. Enable it when asked (or
`hermes plugins enable hermes-filetools`), then restart the gateway **with the desktop client
closed**:

```
hermes gateway restart
```

While the repository is private you need read access (ask for an invite) and git must be able to
sign in to GitHub (Git Credential Manager opens a browser the first time).

## What Hermes' config must have

The plugin works only with ALL of these entries in Hermes' `config.yaml`
(`<HERMES_HOME>/config.yaml`). Hermes writes the `plugins:` part itself when you answer **yes**
during install / enable; `context.engine` you add by hand. Check them if something does not work:

```yaml
plugins:
  enabled:
    - hermes-filetools
  entries:
    hermes-filetools:
      granted_capabilities:
        - tools.override        # REQUIRED: our tools replace Hermes' built-in ones by name
      capabilities_consent:     # written by Hermes when you grant the capability - do not edit
        hash: ...
        granted_at: ...
      allow_tool_override: true # older Hermes versions read this key instead
context:
  engine: ctxctl                # REQUIRED: our context engine - add this line yourself
```

- **`tools.override`** - without it Hermes loads the plugin but keeps its own `read_file` /
  `write_file` / `patch` / `search_files` / `terminal` - silently, no error. To grant it later:
  `hermes plugins enable hermes-filetools --allow-tool-override`.
- **`context.engine: ctxctl`** - without it our context engine is not selected: no compaction
  (button or automatic), no context operations from the panel. Add it by hand, then restart the
  gateway.
- Check: after a gateway restart `<HERMES_HOME>/logs/agent.log` has
  `capability_check plugin=hermes-filetools capability=tools.override decision=allow`.
- Python packages the plugin needs (`python-docx`, `openpyxl`) are installed by Hermes itself on
  install (`pip_dependencies` in `plugin.yaml`).

## Update

```
hermes plugins update hermes-filetools
```

Then restart the gateway with the desktop client closed.

## Your settings

`plugin.yaml` inside the plugin folder holds the DEFAULTS and is replaced by every update. What you
change in the panel (Config → Save) is written to `<HERMES_HOME>/filetools/settings.yaml` — only
the differences from the defaults — and survives updates. All data of the plugin (journal, engine
state, exports) lives under `<HERMES_HOME>/filetools/`.

## Checking compatibility after a Hermes update

Config → Check Seams:

- **Check** — quick: verifies by search that every place this plugin hooks into Hermes' code is
  still there in the Hermes you run now.
- **Full check** — the same plus the behaviour tests shipped with the plugin (`tests/`, pytest,
  about 3 minutes). Use it after every Hermes update.

`FAIL` rows say what will stop working. The tests never touch your data (they run in a temporary
Hermes home).
