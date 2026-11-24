# LKS serial protocol

The implementation uses events from the reconstructed LKS TFT project and observations from the reference TwoTrees SK1. The [event catalog](docs/LKS-EVENTS.csv) lists frames by component across the 80 pages. The [validation plan](docs/LKS-VALIDATION.md) records ambiguous events and hardware dependencies.

| Source | Bytes | Purpose |
|---|---|---|
| Page 38 timer | `91` | Initialization request without a terminator |
| Startup sequence | `88 ff ff ff` | Panel restarted |
| Response to `sendme` | `66 page ff ff ff` | Current page |
| Button event | `65 page action ff ff ff` | The action is the literal sent by the TJC event |
| Keypad or slider | `71 page action lo hi ff ff ff` | Unsigned 16-bit little-endian value |
| Numeric dialog action | `71 page action ff ff ff` | Action without a value |
| Host command | UTF-8 followed by `ff ff ff` | Commands such as `page 3` and `n0.val=29` |

Component IDs in the editor and action IDs on the wire are separate fields. For example, component 4 on Home emits action 1. The parser handles the six-byte LKS format separately from standard TJC touch frames.

| Page | Implemented function | Relevant wire actions |
|---|---|---|
| 0 | Home | 0–4 navigation, 5 light (`b5.picc` is 2 for off and 5 for on), 6 fans, 7 nozzle, 8 bed, 9 network |
| 1 | Keypad | `71` returns the source page and action. Actions 0, 1, and 10 exit the keypad |
| 2 | Print | 5 print menu, 6 light, 7 fans, 8 bed, 9 nozzle, 10 network |
| 3 | Move / Temp | 5–7 step sizes, 8/10 Z, 9 home Z, 11/12 Y, 13/14 X, 15 home XY, 16/17 temperatures. When the target is zero, `n3`/`n2` display the current readings in the idle slots. With a positive target, `n0`/`n1` show current readings and `n3`/`n2` show setpoints |
| 6 | Fans | `71` with actions 0–2 for part cooling, auxiliary, and chamber fans |
| 7 and 9 | Files and file details | 8–10 select a file card on page 7, 11/12 change list pages, 7 on page 9 confirms the print |
| 11 and 14 | System | Action 10 on page 11 opens the second page. Action 11 on page 14 returns to the first. Arrows at the list limits leave the page unchanged |
| 18 | Network | 7–10 select SSIDs, 11/12 change list pages, 14 starts a new scan |
| 19 | Export Diary | Action 7 on pages 14/79 opens this page on the TFT itself. Back (action 5) emits a UART event and requires a return `page` command from the host. Export (action 6) still depends on USB storage |
| 22 and 30 | Wi-Fi keypad and confirmation | Action 0 on page 30 confirms. Action 1 on page 30 and action 0 on page 22 return to Network |
| 27 | Print menu | 0 pauses or resumes, 1 cancels, 2 closes |
| 59–61 | Wi-Fi connection | Progress, success, and failure indicators. The host clears the background before opening these translucent pages. The success message returns to the network list automatically |

The client ignores unmapped actions. The Wi-Fi keypad sends `70 12 action password` without a terminator. A pause in UART traffic delimits the frame. The client accepts the frame only while confirming the selected network and keeps the password out of logs.

When opening the Network page, the host reads `var32_FLDEBE29`, the LKS language variable. The standard TJC numeric reply is `71 value-le32 ff ff ff`. Codes 5 through 12 select Chinese, English, German, Spanish, French, Italian, Korean, and Japanese, respectively.
