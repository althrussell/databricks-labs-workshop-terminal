# Phone, laptop and voice prompts

What an attendee gets when they open Workshop Terminal on a phone, how speaking a prompt works,
and what an operator needs to know when either goes wrong.

## What the attendee gets

| | Laptop | Phone or tablet (touch screen) |
| --- | --- | --- |
| Prompt box | Opens from the mic icon in the agent bar; close it with ×. | Always docked under the terminal. |
| Speak a prompt | Mic icon (Chrome, Edge, Safari). | Mic in the prompt box, or the keyboard's own dictation key. |
| Terminal keys | The hardware keyboard. | A key bar: Esc, Tab, ⇧Tab, arrows, ⏎, ^C, and ⌨ to type in the terminal itself. |
| Open on phone | Phone icon in the header shows a QR code of this app. | Hidden. |

The prompt box shows the text before anything is sent. **Send** (or Enter) pastes it into the
running agent and presses Enter, exactly as if the attendee had typed it. Shift+Enter adds a
line. Nothing reaches the agent until the attendee sends it.

On a phone the bars above the terminal shrink to one row each in the agent view. While the
keyboard is open, they step aside so the terminal and the prompt box fill the screen.

## How voice works, and where the audio goes

- The mic uses the browser's built-in speech recognition (the Web Speech API). The browser
  vendor's speech service turns audio into text: Google for Chrome, Microsoft for Edge, Apple for
  Safari.
- **WT never receives, stores or logs audio.** What comes back is text in the prompt box. Once
  sent, it is ordinary terminal input with the same handling as typing. Nothing new is captured
  for Control Tower.
- Firefox has no speech recognition, so it shows no mic. Typing in the prompt box still works.
- The mic needs a secure page (https, which every deployed app is) and the attendee's permission
  the first time.
- Recognition uses the browser's language (`navigator.language`).
- On iOS, listening can stop after a pause. The words heard so far stay in the box.

## One session, two devices

The phone and the laptop open the same seat and the same live terminal; neither replaces the
other. The terminal has one size, so **the device the attendee types on, or comes back to,
gets its size**, and the agent redraws for that screen. The other device may show wrapped
output until it is used again.

**Keep the laptop tab open while using the phone.** The Omnigent sign-in lives in an open
Workshop Terminal tab ([attendee-messaging.md](./attendee-messaging.md)). A locked phone
suspends its tab, so a phone-only attendee can see Omnigent go stale. This is expected from the
tab-bound design and has not been observed live yet. Claude Code and Codex are unaffected.

To move to a phone, the attendee selects **Open on phone** on the laptop, scans the code, and
signs in to the workspace on the phone with the same account.

## When something goes wrong

| The attendee says | Answer |
| --- | --- |
| "There's no mic button." | Firefox, or a browser without speech recognition. Use the phone keyboard's dictation key, or type. |
| "It says the microphone is blocked." | Allow the microphone for this site in the browser's site settings, then tap the mic again. |
| "The phone asks me to sign in." | Expected: the phone needs its own workspace sign-in, with the same account as the laptop. |
| "Text looks garbled on my laptop after using my phone." | Type anything, or click into the laptop tab: the laptop takes the size back. |
| "The agent didn't react to Send." | Check the agent is running (agent bar). Send needs a connected terminal and shows a notice if it is not. |

## Code and tests

- Prompt box and mic: `frontend/src/components/PromptComposer.tsx`, `frontend/src/voice.ts`.
- Key bar: `frontend/src/components/TouchKeyBar.tsx`, `frontend/src/touchKeys.ts`.
- Keyboard-aware layout: `frontend/src/viewport.ts`; the effect in `frontend/src/App.tsx` sets
  `--app-height` and `data-keyboard`.
- QR code: `frontend/src/components/OpenOnPhone.tsx`, `frontend/src/qr.ts`.
- Send and keys go through `frontend/src/components/TerminalView.tsx` (`submit`, `press`).
  Terminal size ownership is in `server/ws.py::session_socket`.
- Tests:
  - `tests/test_sessions.py::test_typing_device_owns_terminal_size`;
  - `tests/test_mobile_rendered.py`, phone and laptop, with a fake speech service;
  - `frontend/src/voice.test.ts`, `frontend/src/touchKeys.test.ts`,
    `frontend/src/viewport.test.ts` and `frontend/src/qr.test.ts`.

## Not yet verified on real devices

The rendered tests emulate a phone in Chrome. Before relying on this at an event, check once on
a deployed instance:

1. iPhone (Safari) and Android (Chrome): scan the QR code, sign in, and launch Claude Code and
   Codex.
2. Dictate a prompt and send it. Check that each agent submits it as one prompt; Send waits
   120 ms between the paste and Enter.
3. Answer an agent menu with ↑/↓/⏎ and back out with Esc.
4. Open the keyboard: the prompt box stays visible above it.
5. Scroll agent output with a finger.
6. With the laptop tab still open, type on each device in turn: each redraws cleanly.
7. Run `curl -sI <app-url>` and confirm no `Permissions-Policy` header blocks `microphone`.
