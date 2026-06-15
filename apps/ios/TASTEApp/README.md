# TASTE iOS App

This app is a thin iOS client for the TASTE Web/API server. It lets an iPhone or iOS Simulator create/select a TASTE project, monitor live workflow progress, follow project blockers and next actions, inspect the latest Claude project-agent response tail, start/cancel TASTE project actions, update the Find-stage LLM config, and save remote runtime paths for the project worker.

The phone is not the research runtime. TASTE still runs on your computer or server, and large artifacts such as repositories, datasets, logs, generated papers, and PDFs stay on that machine.

## Requirements

- Xcode with iOS 17 SDK support.
- A running TASTE Web/API server.
- Network access from the simulator or device to the TASTE server.

## Start the TASTE Server

From the repository root:

```bash
conda activate taste
scripts/start_web.sh
```

The default server address is:

```text
http://127.0.0.1:8765
```

For a physical iPhone on the same trusted LAN, bind the server to the LAN interface:

```bash
WEB_HOST=0.0.0.0 scripts/start_web.sh
```

Then use:

```text
http://<computer-or-server-lan-ip>:8765
```

On a physical iPhone, do not enter `http://127.0.0.1:8765` or `http://localhost:8765`; those addresses point back to the phone. Use the computer's LAN IP, a VPN address, or an authenticated tunnel URL. The app's readiness panel shows this warning before remote workflow buttons are enabled.

Do not expose the TASTE API directly to the public internet without a tunnel, VPN, firewall, or reverse proxy with authentication.

For profiles marked `Cloud`, the app requires an `https://` URL or an authenticated tunnel so the server access token is not sent over plain HTTP. Local LAN debugging can still use the `Computer` profile type with `http://<LAN-IP>:8765`.

For mobile API smoke tests on a machine without the built web frontend or `npm`, start only the API surface:

```bash
WEB_API_ONLY=1 scripts/start_web.sh
```

For LAN, server, or cloud access from an iPhone, protect the TASTE API with a server access token:

```bash
TASTE_SERVER_ACCESS_TOKEN=<server-token> WEB_HOST=0.0.0.0 scripts/start_web.sh
```

Then save the same value in the app's `Server access token` field. The token is sent as `Authorization: Bearer <server-token>` and is stored in the iOS Keychain. If the app shows `TASTE server rejected the server access token`, update `Server access token` in Settings and tap `Test Connection` again.

`Test Connection` also checks `/api/config/meta` for `mobile_api_version` and `mobile_capabilities`. If the app says the server does not advertise the mobile control-plane API, update `branch-app` on the computer/server and restart `scripts/start_web.sh`.

To avoid typing the server URL and token on the phone, generate a connection link on the TASTE computer/server and open it on the iPhone. `--server-url auto` detects this machine's LAN IPv4 address, which is the common path when the Mac/server and iPhone are on the same trusted network:

```bash
python scripts/mobile_connection_link.py --server-url auto --port 8765 --profile "Lab Mac" --kind computer --project ios_e2e_mobile_app --token <server-token>
```

If the detected address is not reachable from the iPhone, pass the exact LAN, VPN, or tunnel URL instead: `--server-url http://<computer-or-server-lan-ip>:8765`. Add `--json` when another tool needs structured output; the JSON includes the full `link`, a `redacted_link` that does not expose the token, and a token-free `connect_page_url` for opening the `/mobile/connect` helper page directly on the iPhone.

When copying a long terminal link is awkward, open the lightweight helper page on a trusted device:

```text
http://<computer-or-server-lan-ip>:8765/mobile/connect?profile=Lab%20Mac&kind=computer&project=ios_e2e_mobile_app
```

The page works even when the built web frontend is absent. It shows an inline QR code that can be scanned from Settings -> `Scan Connection QR` in the iOS app. It does not print `TASTE_SERVER_ACCESS_TOKEN` into the HTML; if a token is needed, type it into the page on a trusted device and the page refreshes the inline QR through the same TASTE server. You can still tap `Open in TASTE` or copy the generated `Connection link` and tap `Import From Clipboard` in the iOS app's Settings. The `connect_page_url` from `scripts/mobile_connection_link.py --json` is this same no-token page URL, suitable for sending to the phone through AirDrop, chat, or MDM.

The app handles `taste://connect?...` links, imports the connection profile, stores the token in Keychain, selects the optional project, and immediately runs a lightweight connection test. Treat a link that includes `token=` as a secret. If iOS shows an "Open in TASTEApp?" prompt for a custom link, copy the full link and tap Settings -> `Import From Clipboard`, or scan the page QR from Settings -> `Scan Connection QR`; manual paste into `Connection link` plus `Import Connection Link` remains available as a fallback. These in-app paths stay inside the app and do not depend on the external Open confirmation.

From another terminal, verify the control-plane endpoints used by the iOS app:

```bash
python scripts/mobile_api_smoke.py --server-url http://127.0.0.1:8765 --project-id ios_e2e_mobile_app --token <server-token>
```

Add `--llm-probe` when you also want to call `/api/config/llm-probe` and include the sanitized Find LLM/DeepSeek diagnostic result in the summary. The probe is diagnostic only, so a temporary external model-gateway failure does not make the mobile control-plane smoke fail. `llm_probe_diagnostic.category=network_unreachable` points to VPN, campus-network, firewall, or proxy reachability; `auth_failed` points to the saved API key. Add `--light-action status` or `--light-action healthcheck` only when you also want to dispatch one lightweight project job. Add `--wait-light-actions` to poll `/api/jobs/{job_id}` until `done`, `error`, `cancelled`, `blocked`, or timeout:

```bash
python scripts/mobile_api_smoke.py --server-url http://127.0.0.1:8765 --project-id ios_e2e_mobile_app --token <server-token> --light-action healthcheck --wait-light-actions
```

The wait summary keeps only short progress/log tails in the script output; compute, full logs, and artifacts remain on the TASTE server. If the server token is not enabled, omit `--token`. For a protected tunnel, VPN gateway, or reverse proxy, use the same `--token` or set `TASTE_SERVER_TOKEN`.

The app includes a local-network permission purpose string for iOS. On first LAN access, iOS may ask whether TASTE can connect to devices on the local network.

For a physical iPhone path, run the device preflight before treating the setup as ready:

```bash
python scripts/ios_device_preflight.py --server-url http://<computer-or-server-lan-ip>:8765 --project-id ios_e2e_mobile_app --token <server-token> --profile "Lab Mac" --kind computer
```

The preflight uses `xcrun devicectl` to find a connected and trusted physical iPhone, blocks `127.0.0.1/localhost` URLs that only point back to the phone, runs `mobile_api_smoke.py` to verify `mobile_api_version`, and prints a token-free `connect_page_url` for the `/mobile/connect` helper page. It does not replace installation on a real device; code signing, provisioning, Xcode run, or TestFlight distribution still depend on your Apple developer setup. When something is missing, the command exits non-zero and lists `blocking_items` in the JSON output.

When a paired iPhone and Apple development signing are available, run a physical-device smoke that builds, installs, launches, and passes a connection payload URL into the app:

```bash
python scripts/ios_device_smoke.py --server-url http://<computer-or-server-lan-ip>:8765 --project-id ios_e2e_mobile_app --token <server-token> --profile "Lab Mac" --kind computer --allow-provisioning-updates
```

To check signing readiness without entering Xcode build/install, add:

```bash
python scripts/ios_device_smoke.py --server-url http://<computer-or-server-lan-ip>:8765 --project-id ios_e2e_mobile_app --token <server-token> --profile "Lab Mac" --kind computer --signing-preflight-only
```

Add `--development-team <TEAMID>` or set `DEVELOPMENT_TEAM=<TEAMID>` if Xcode cannot infer the signing team. If your provisioning profile does not cover the default `org.ustcstarteam.taste.mobile`, add `--bundle-id <your.bundle.id>` or set `TASTE_IOS_BUNDLE_ID`. The signing preflight reports the Apple Development identity, team id, matching provisioning profile count, and blockers. The full smoke runs the device/API preflight first, builds for `iphoneos`, installs with `xcrun devicectl device install app`, launches with `xcrun devicectl device process launch --payload-url`, and redacts any token embedded in the generated `taste://connect` link.

To verify the iOS app itself on a simulator, build, install, launch, screenshot, and run the lightweight API smoke in one pass:

```bash
python scripts/ios_app_smoke.py --device-name "iPhone 17" --server-url http://127.0.0.1:8765 --project-id ios_e2e_mobile_app
```

To include the connection-link import path in the simulator smoke, generate a link and pass it to the smoke script:

```bash
CONNECTION_LINK=$(python scripts/mobile_connection_link.py --server-url http://127.0.0.1:8765 --profile "Simulator TASTE" --kind computer --project ios_e2e_mobile_app)
python scripts/ios_app_smoke.py --device-name "iPhone 17" --server-url http://127.0.0.1:8765 --project-id ios_e2e_mobile_app --connection-link "$CONNECTION_LINK" --connection-link-dispatch launch_argument --light-action healthcheck --wait-light-actions
```

The script selects an explicit iPhone simulator UDID instead of the generic `booted` shortcut, so a booted Apple Watch simulator cannot accidentally receive the app install. If `--connection-link` includes a token, the JSON summary redacts it. `--connection-link-dispatch launch_argument` is the automation-friendly path: it reboots an already booted target simulator and imports the connection through a launch argument so stale system URL prompts cannot pollute the screenshot. Use the default `openurl` dispatch when you specifically want to exercise the iOS custom-link confirmation prompt. On a real device, tap `Open` when iOS asks to open TASTEApp.

## Run the iOS App

Open the Xcode project:

```bash
open apps/ios/TASTEApp/TASTEApp.xcodeproj
```

Select the `TASTEApp` scheme and run it on an iOS Simulator or device.

You can also verify from the command line:

```bash
cd apps/ios/TASTEApp
swift test
xcodebuild -project TASTEApp.xcodeproj -scheme TASTEApp -configuration Debug -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' build CODE_SIGNING_ALLOWED=NO
```

## App Flow

- `Settings`: review run readiness, save/select connection profiles for a local computer, LAN server, or cloud worker, test the connection, create a project on the server, sync the project's research interest, researcher profile, venue, and title, refresh projects, load/sync/probe the Find LLM provider/base URL/model/API key status, and save/detect remote runtime paths.
- `Dashboard`: view the selected project, project-summary stage states, current blockers/next actions, the latest Claude response tail for the current project or a specific Environment/Experiment/Paper stage, remote PDF/TeX artifact links, the seven TASTE workflow stages, live/recent jobs, inspect compact job details/log tails, and cancel queued/running jobs.
- `Run`: review readiness, set run context, and start the full workflow, Find, the current-Find Read/Idea/Plan bridge, Environment, Experiment, Paper, or the current-Find selection route. Current-Find actions stay disabled until the selected project has a completed or selected Find packet. Lightweight status and healthcheck requests run immediately; actions that dispatch remote research work show a confirmation dialog before they start on the TASTE server.

Connection profiles store only a profile name, target type, and normalized TASTE server URL. Tap `New Connection` before entering a new computer, server, or cloud worker address so the new target is saved as its own profile instead of replacing the selected one. If the TASTE server is behind a tunnel, VPN gateway, or reverse proxy that expects bearer authentication, save the optional server access token with the profile; the token is kept in the iOS Keychain and sent as an `Authorization` header, not encoded into the profile JSON. Switching profiles clears stale project/job state from the previous server before the next refresh. `Sync Project Profile` writes the selected project's research interest, researcher profile, target venue, and paper title to `/api/projects/{project}/config`; those fields are also lightly synced before non-status workflow launches so Find/Read/Ideas/Plan use the current mobile profile. `Load Find LLM Config` reads provider/base URL/model and saved-key status from the server without bringing the secret key onto the phone. The AI API key field is sent to the TASTE server when you tap sync or probe, then cleared from the app form. The app persists only lightweight local settings such as connection profiles, project selection, provider/base URL/model, research profile text, and run text fields.

## Runtime Configuration

The `Remote Runtime` section writes to the TASTE server's project runtime API. These paths describe the computer/server environment, not phone-local paths:

- `Claude path`: Claude Code executable path on the TASTE machine.
- `Management Python`: Python used to run TASTE Web and orchestration scripts.
- `Node bin`: Node.js bin directory used by the TASTE machine.
- `Conda env` and `Conda base`: experiment environment hints for project stages.
- `Experiment Python`: Python used by project experiments.
- `Extra PATH entries`: additional server-side PATH entries, separated by `:`, comma, or newline.

`Load Runtime` reads the selected project's current server-side runtime configuration into the form. `Detect and Load Runtime` asks the server to inspect its own runtime and then fills any detected paths back into the app. `Save Runtime` persists the fields above on the selected project and reloads the server response. The runtime diagnostics summary shows the server's own Python, Claude, Node, npm, Conda, and PATH checks; critical failures such as missing Claude or management Python block expensive remote stages until fixed.

## Progress Following

The app refreshes in the background while open. It polls about every 8 seconds when a queued/running job is visible and about every 30 seconds when no job is active, which keeps progress fresh without busy-polling the TASTE server from the phone. Refresh pulls both compact job rows and the selected project's stage summary, blockers, next actions, and remote artifact links, so the Dashboard can still show Environment/Experiment/Paper progress when no live job is currently running. When a server access token is saved, protected `/api/projects/.../files/...` artifact rows open inside TASTE with the saved bearer token instead of launching an unauthenticated browser tab. Use the Dashboard refresh button when switching networks or after restarting the TASTE server.

The Dashboard `Claude Response` controls call `GET /api/projects/{project}/claude/latest-response` with a mobile cap of 16,000 characters. The sheet shows the returned tail, stage/source metadata, and whether the response was truncated. Full Claude receipts and project logs remain on the TASTE server.

Each job row can open a compact detail sheet. The sheet requests `GET /api/jobs/{job_id}?compact=1`, shows the current progress summary, and keeps only a bounded log tail on the phone.

The `Run Readiness` section in Settings checks the phone-side prerequisites for a full remote workflow: reachable TASTE server, selected project, Find LLM fields, remote runtime hints, and lightweight phone storage policy.

The Run tab uses the same readiness checks to prevent accidental remote work. Lightweight status and healthcheck actions can run once a server URL and project are present. Find requires the Find LLM fields, while full workflow and later-stage actions stay disabled until the remote runtime is detected or saved. `Read Current Find`, `Generate Ideas`, `Draft Plans`, and `Select Current Find Plan` also require a completed or selected Find packet for the project; if there is no current Find yet, the buttons point the user back to Find instead of launching a failing server job. These actions call the TASTE server's current-Find bridge, so Claude Code, full-text reading, idea generation, and plan artifacts remain on the computer/server. Disabled buttons show the reason directly below the action.

Before starting any non-lightweight action, the app asks for confirmation that the work will run on the configured TASTE server while the phone remains a compact progress-following control plane. This guards against accidental remote compute from a small touch target.

## Mobile Storage Policy

The app is intentionally a compact control plane:

- Local cache budget is capped at 20 MB by default.
- Cacheable data is limited to summaries, project rows, and job lists.
- API calls use an ephemeral mobile URL session with zero disk cache and a small memory cache.
- PDFs, TeX files, datasets, cloned repositories, experiment logs, and generated research artifacts remain on the TASTE server. Protected file previews use a temporary Quick Look file capped by the 20 MB budget and clear the previous preview before writing the next one.

After a simulator or device build, check the app bundle plus phone-side cache budget:

```bash
python scripts/ios_app_footprint.py --app <path-to>/TASTEApp.app
```

The default gate keeps the app bundle under 50 MB, any single bundled file under 10 MB, and the estimated maximum phone-local footprint under 75 MB including the 20 MB cache budget.

This keeps the mobile client usable without turning the phone into a research workspace.
