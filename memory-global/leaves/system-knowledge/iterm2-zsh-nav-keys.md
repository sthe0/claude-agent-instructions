---
name: iTerm2 + zsh text navigation keybindings
description: Difficulty it removes — Option/Cmd+arrow / Home / End print escape sequences in the user's iTerm2 + zsh. Fact — both an iTerm profile setting (Option Key Sends Esc+ / Natural Text Editing) AND a zsh bindkey block are required, neither alone suffices; cat -v diagnoses which half is broken.
type: reference
schema: regularity/v1
converted_from: leaf/v1
given: measurement
given_source: ~/Library/Preferences/com.googlecode.iterm2.plist
created: 2026-05-29
last_verified: 2026-06-24
---

# iTerm2 + zsh navigation keys: two-part fix

## Difficulty

Option+←/→, ⌘+←/→, Home, End, Option+Backspace print literal characters (`Æ`, `^[b`, `^[[H`, …) into the prompt instead of performing word/line navigation. The cause is two-part and fixing only one half leaves the bug — the iTerm2 profile must send the right escape sequences AND zsh must have `bindkey` entries for them.

## Regularity

Observed on the user's Mac (iTerm2 with zsh, TERM=xterm-256color): Option+arrow, Cmd+arrow, Home, End and Option+Backspace printed literal characters at the prompt (the leaf lists Æ, ^[b and ^[[H) instead of navigating. Before the fix on 2026-05-29, defaults read com.googlecode.iterm2 piped to grep "Option Key Sends" showed = 0 for both of the user's profiles. The leaf reports the issue as resolved by two changes made together on that Mac: setting the iTerm2 profile values Option Key Sends and Right Option Key Sends to Esc+ (value 2), and adding a bindkey block to ~/.zshrc for the sequences the leaf lists (for example ^[[1;3D, ^[[1;5D, ^[[H, ^[[F, ^[[1~, ^[[4~, ^[OH, ^[OF, ^[^?); the leaf labels ^[[1;3D and ^[[1;3C in that block as the sequences of the iTerm2 Natural Text Editing preset, and gives the preset as one GUI route to the iTerm2 half of the fix. The leaf does not record testing either change alone. The leaf states, without recording an observation of them, that Normal mode (value 0) sends a macOS-native UTF-8 byte such as Æ, that zsh's default emacs keymap binds only the ^[b and ^[f Meta forms, and that iTerm2 overwrites its plist ~/Library/Preferences/com.googlecode.iterm2.plist on quit. As a diagnostic, the leaf reports that running cat -v and pressing the broken key shows the bytes iTerm2 sends: a printable character such as Æ is read by the leaf as the iTerm2 profile half being wrong, and a ^[... sequence as the zsh binding half being missing.

## Given

Given from measurement: `~/Library/Preferences/com.googlecode.iterm2.plist`. The statement is taken as known from that source, not induced from recorded experience.

## Refutation

On the user's Mac, setting only the iTerm2 profile to Esc+ with no bindkey entries (or only the bindkey block with Option Key Sends at 0) already makes Option+arrow navigate words, or cat -v shows ^[... for a key while the profile is at 0.

## Guidance

User's Mac: iTerm2 + zsh, `TERM=xterm-256color`.

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

## See also
