# ccatv

ccatv is a Linux television application.

Current project direction:

- Python GTK4 user interface designed for across-the-room TV use
- Playback backend abstraction with:
	- libmpv as the first implementation
	- GStreamer as an optional/alternate backend
- TV recorder and tuner control via external dvbstreamer + dvbctrl
	- local reference path: ~/src/dvbstreamer/dvbstreamer-2.1.0
- TV guide and schedule metadata from Schedules Direct
- Remote control support via a Windows Media Center remote and inputlirc
- Jellyfin integration for media library access on the local network
- GPLv3 project licensing intent

## TvRecorder Configuration

TvRecorder persists local dvbctrl credentials under
`$XDG_CONFIG_HOME/dvbstreamer/userconfig.json`.

If `XDG_CONFIG_HOME` is unset, this resolves to
`$HOME/.config/dvbstreamer/userconfig.json`.

Current file shape:

```json
{
	"password": "your-password",
	"username": "your-username"
}
```

Both `dvbstreamer` and `dvbctrl` read auth directly from this file.

To create or update the local file interactively, run `uv run ccatv-setup`.

You can also use the shared CLI entrypoint with `uv run ccatv setup`.

ccatv runtime connection settings are stored separately in:
`$XDG_CONFIG_HOME/ccatv/runtime.json` (fallback `$HOME/.config/ccatv/runtime.json`).

Current runtime file shape:

```json
{
	"dvb_adapter_count": 1,
	"dvbstreamer_host": "localhost"
}
```

You can override these with CLI setup flags:
- `uv run ccatv setup --host druidmedia --adapter-count 4 --username your-user`

See docs/architecture-proposal.md for the proposed architecture and phased implementation plan.
