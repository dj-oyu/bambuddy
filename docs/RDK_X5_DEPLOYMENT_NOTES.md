# RDK X5 deployment notes

2026-11-27: `/app/bambuddy` was at `9d1a83440`, while `/opt/bambuddy/.deployed-rev` recorded `8bef58ca`. The actual running backend and static files differed from that recorded revision; the running frontend bundle was also different from the checkout. The deployment marker therefore does not reliably identify the running version. Running the checkout's `deploy.sh` would deploy substantial unrelated changes.

For the initial CRC/frame error label fix, the current source change was tested and built in an isolated staging directory. Because the running bundle had no verified matching source revision, a temporary compatibility hotfix was applied to the actual served BMCU bundle: only the error label and its red styling were changed, new bundle and entry filenames were created, and the HTML entry point was replaced atomically. Existing assets were retained. The original entry point is `/opt/bambuddy/static/index.before-bmcu-label-hotfix.html`; restoring it to `/opt/bambuddy/static/index.html` rolls back the hotfix. This is not a deployment of the PR commit and does not update `.deployed-rev`.

A later normal deployment should first reconcile the live backend, source checkout, static assets and deployment record, then build and deploy a reviewed origin revision as a consistent set. Preserve runtime data and `/app/bambuddy/handover.md`.

Windows sandbox access to Tailscale and SSH required elevated execution. The local frontend dependencies were unavailable, so validation used the RDK's existing dependencies in a separate staging directory. The hotfix did not require a service restart; the page and modified asset returned HTTP 200.
