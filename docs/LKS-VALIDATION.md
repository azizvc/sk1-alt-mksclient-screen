# LKS event validation

The [event catalog](LKS-EVENTS.csv) has one row per event declaration in the editable panel project. Each row records the page, component, event, number of lines, UART frames, navigation handled by the display, and functional area. The HMI assigns component IDs and wire action IDs independently. The client routes events using the emitted frame.

| Measure | Count |
|---|---:|
| Pages | 80 |
| Event declarations | 2,988 |
| Events that transmit over UART | 497 |
| Events with local logic | 427 |
| Empty declarations | 2,064 |
| Distinct UART signatures | 489 |

A UART event may request a host action, notify the host about an action the display already performed, or report a touch with no effect at a navigation boundary. See the [protocol reference](../PROTOCOL.md) for startup frames, host query responses, and TJC acknowledgments.

## Coverage by functional area

| Area | Pages and scope | Checks |
|---|---|---|
| Printing | 0–2, 7–10, 18, 22–28, 30–31, 38, 43–44, 54, 59–62, 73–74, 76–77. Printing, files, history, dialogs, network, and startup | Simulate files and print status, test ambiguous dialogs, and check cancellation, keypad sequencing, and password redaction |
| Operation | 3–6, 33–37, 42, 45–51, 55–58. Movement, fans, filament, leveling, and calibration | Test limits, homed axes, extrusion temperature, and a busy printer. Simulate success, failure, and missing Moonraker capabilities |
| System | 11–17, 19–21, 29, 32, 39–41, 52–53, 63–72, 75, 78–79. System, language, support, diagnostics, maintenance, and power | Check local transitions, modal dismissal, and confirmation. Simulate restart and shutdown requests and record dependencies on factory services or files |

## Validation sequence

1. **Check the source of each event.** Regenerate the catalog for both panel projects. Find the component and compare the page, action, and frame length. When the HMI does not establish the meaning of a host action, check the factory client or reproduce the event in a simulator. Leave the meaning pending if the evidence only confirms the existence of a frame.
2. **Test the parser.** Send complete, fragmented, and concatenated frames. Cover numeric values, unterminated startup and Wi-Fi password frames, local brightness controls, and unknown payloads. Check that ambiguous frames do not trigger protected operations.
3. **Test event routing in process.** Inject simulated Moonraker, clock, and serial connections. Record the initial page and state, minimum data, expected UART output and RPC calls, and final state. Repeat outside the source page, with Moonraker disconnected, after reconnecting, and with controls disabled. Check that actions performed on the TFT synchronize the host's page state without repeating the action.
4. **Check both API versions.** Use read-only requests on the reference SK1 and repeat the workflows on current Klipper and Moonraker. Record software versions, objects, and endpoints. Verify the response when an object, macro, or endpoint is unavailable.
5. **Check the physical UART in read-only mode.** With the printer idle, open real pages and check `sendme`, acknowledgments, and attributes read through `get`. Verify list boundaries, modal dismissal, language selection, IP updates, and page transitions. For Wi-Fi, use a simulated connection before switching networks.
6. **Validate physical controls.** Check the prerequisites for each workflow before enabling controls. Start with reversible operations and a small move on an already-homed axis. Verify homing, extrusion, calibration, and endstops individually. Use separate success criteria for filament handling, bed operations, saved profiles, power actions, and printing. Check that failure and cancellation leave the printer recoverable.
7. **Check workflows with external dependencies separately.** Verify the files written to a test USB drive. Updates and restoration require an image, a rollback procedure, and spare media. Linking an external service requires a test account. Record an observable result and the service version for each check.

## Shared frames

| Source | Shared frame | Check |
|---|---|---|
| Pages 4 and 42, `b10` and `b11` | Action 4 on each page | Check the common destination in the factory client. Distinguishing the buttons requires separate IDs in the HMI |
| Page 11, brightness decrease and increase | Unterminated `dim` value | Check that brightness is stored locally and the next UART frame is intact |
| Pages 23, 24, and 25, page and modal background | Action 2 on each page | Check that both dismiss the modal |
| Page 31, network **Delete** and **Edit** buttons | Action 0 | Generate a test TFT with separate action IDs before distinguishing delete from edit on the host. Action 2 already identifies the return event |
| Page 45, touch and timer | Action 0 | Check that completion runs once when the user touches the panel or the timer fires |

## Implementation and outstanding checks

| Area | Implemented | External dependencies or outstanding checks |
|---|---|---|
| Files and printing | G-code directories, file-card menus, metadata, print and delete confirmation, pause, resume, cancellation, history, and last-print results with guarded reprinting | Pages 43/54 have USB workflows. `gcodes/sda1` returned HTTP 400 on the reference SK1 without mounted media. USB printing and physical display of ColPic thumbnails still need verification. Page 10 rows emit no touch actions. Actions 0/1 on page 26 and action 0 on page 76 need a confirmed error or sensor source |
| Filament and calibration | Page 4 actions 5/6 use `LOAD_MATERIAL` and `UNLOAD_MATERIAL`. Homing, motor release, Z tilt, mesh, input shaping, and manual probe calibration check printer state and limits | `printer.gcode.help` confirmed both filament macros and `Z_TILT_ADJUST`, `BED_MESH_CALIBRATE`, `SHAPER_CALIBRATE`, `PROBE_CALIBRATE`, and `Z_OFFSET_APPLY_PROBE` on the reference SK1. Physical execution is still pending |
| System and power | Menus, **About**, support tabs, language and sleep synchronization, and protected stop and recovery actions. **Export Diary** (19) returns to source page 14 or 79 even if the host missed the local page transition | Vendor updates at `11:13`, `39:1`, `57:0`, and `79:9`. Diagnostics at `16:5,6,8,9`. USB exports at `19:6` and `63:6` need mounted media. Restoration at `20:6–10` and `75:6`, and Obico linking at `78:6`, still need validation |
| Network | Pagination, selection, confirmation, UART keypad, dialog return, IPv4 lookup, and access to calibration without starting movement. Wi-Fi scanning queries the local WPA control socket and accepts open or WPA/WPA2 Personal networks | Both buttons on page 31 emit `0x65 31 0`. Separate IDs must be tested before enabling operations on that page |

The automated replay covers **492 frames with fixed pages and actions** in read-only mode. Separate parser tests cover the five remaining UART events from the numeric keypad, Wi-Fi keypad, initialization, and two brightness buttons. The replay checks that those frames can be processed offline. Individual tests and the physical checks above cover action outcomes.

The live probe succeeded on the reference SK1 with Klipper 0.14.0 and Moonraker 0.11.0. See the [README](../README.md#compatibility) for the full reported versions and the scope of the check.

## Regenerate the catalog and run tests

The catalog was generated from `LKS-Ambar-v2.HMI` with SHA-256 `4874331dcba96954f6e96468eec17fc1d8a55efe90d0b0ae0db376666fdcb3d0`. Regeneration requires that HMI file and a checkout of the `nxwright` decoder with the Python package on its import path. The checked-in CSV is sufficient for running the tests.

```sh
python tools/inventory_lks.py --source "/path/to/LKS-Ambar-v2.HMI" --decoder "/path/to/nxwright"
python -m unittest discover -s tests -v
```
