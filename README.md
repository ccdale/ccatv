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

See docs/architecture-proposal.md for the proposed architecture and phased implementation plan.
