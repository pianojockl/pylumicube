# Home Assistant side

The cube publishes its own MQTT discovery, so the entities appear by
themselves (see `../README.md` §4). These files are the *reference* for the
things that live in Home Assistant rather than in this repo, so the setup can
be reviewed and recreated: they were written through the HA API
(`script/config/save`, `lovelace/config/save`), not by editing `.storage/`.

| File | What it is |
| --- | --- |
| `scripts.yaml` | The `lumicube_*` scripts: say, scroll, screen text, tone, stop |
| `dashboard.yaml` | The "LumiCube" dashboard (`/lumicube-controls`) |
| `input_text.yaml` | The helper the say button reads its sentence from |

## How the pieces fit

```
input_text.lumicube_say_text   you type the sentence here
select.lumicube_speak_language which language to speak it in (from the cube)
script.lumicube_say            publishes it to lumicube/speaker/say
```

The script's `sentence` field falls back to the text box, so both work: press
the button on the dashboard, or run the script from an automation with the
fields filled in. An empty `language` field means "use the cube's selected
language" — that is why the select lives on the cube and not in the script.

`select.lumicube_speak_language` sets the cube's *default* speak language, so
it also affects `text.lumicube_speak` and anything else that speaks without
naming a language. It lasts until the daemon restarts; for a permanent default
set `PYLUMICUBE_SPEAK_LANGUAGE=de` (or `--speak-language de`) on the daemon.
