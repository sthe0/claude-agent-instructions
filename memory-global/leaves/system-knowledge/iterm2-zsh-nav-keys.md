---
name: iTerm2 + zsh text navigation keybindings
description: When Option/Cmd+arrow / Home / End print escape sequences in the user's iTerm2 + zsh setup, both an iTerm profile setting AND zsh bindkeys are required — neither alone suffices.
type: reference
---

# iTerm2 + zsh navigation keys: two-part fix

User's Mac: iTerm2 + zsh, `TERM=xterm-256color`. When Option+←/→, ⌘+←/→, Home, End, Option+Backspace print literal characters (`Æ`, `^[b`, `^[[H`, …) into the prompt, the cause is **two-part**, and fixing only one half leaves the bug:

1. **iTerm2 profile**: `Option Key Sends` and `Right Option Key Sends` default to `0` (= "Normal"). In Normal mode, Option+Left sends a macOS-native UTF-8 byte (e.g. `Æ`) that no shell binding can intercept. Must be changed to `Esc+` (value `2`).
   - GUI path: iTerm2 → Settings → Profiles → \<profile\> → Keys → Key Mappings → **Presets → Natural Text Editing**. One click per profile; covers Option+arrow, Cmd+arrow, Home/End, Option+Backspace.
   - Plist path (`~/Library/Preferences/com.googlecode.iterm2.plist`): values `Option Key Sends` / `Right Option Key Sends` per profile. **Don't edit live** — iTerm overwrites the plist on quit; either quit first, or use GUI.
2. **zsh ZLE**: even with the right escape sequences arriving, zsh's default emacs keymap only binds the `^[b`/`^[f` Meta-b/f forms. Need `bindkey` entries for the modern sequences. Lives in `~/.zshrc`.

Comprehensive `bindkey` block that covers iTerm2 Natural Text + Esc+ + Linux Ctrl+arrow + Home/End variants:

```zsh
bindkey -e
# word jump
bindkey '^[^[[D' backward-word        # iTerm Esc+, no preset
bindkey '^[^[[C' forward-word
bindkey '^[[1;3D' backward-word       # iTerm Natural Text Editing
bindkey '^[[1;3C' forward-word
bindkey '^[[1;5D' backward-word       # Ctrl+arrow (Linux)
bindkey '^[[1;5C' forward-word
bindkey '^[b'    backward-word        # Meta-b
bindkey '^[f'    forward-word
# line begin/end
bindkey '^[[H'  beginning-of-line
bindkey '^[[F'  end-of-line
bindkey '^[[1~' beginning-of-line
bindkey '^[[4~' end-of-line
bindkey '^[OH'  beginning-of-line
bindkey '^[OF'  end-of-line
# delete word backward
bindkey '^[^?'  backward-kill-word
```

**Diagnostic shortcut**: `cat -v` then press the broken key → you'll see the exact bytes iTerm sends. If you see a printable char (like `Æ`) the iTerm profile is wrong (fix #1). If you see `^[…` the shell isn't binding it (fix #2).

> verified by: live fix on 2026-05-29 on the user's Mac. `defaults read com.googlecode.iterm2 | grep "Option Key Sends"` showed `= 0` for both profiles before fix.
