# Voices

Kokoro ships 54 presets. Naming is `<lang><gender>_<name>`: `af_heart` = American
Female "Heart". The `lang_code` must match the voice prefix or pronunciation
degrades.

Run `md_whispr.py voices` for the short list from the CLI.

## American English — `--lang a`

| Voice | Character |
|---|---|
| `af_heart` | Warm, natural, unhurried. **Default.** Best for long technical docs. |
| `af_bella` | Brighter, more energy. Good for marketing copy and briefs. |
| `af_nova` | Crisp, newsreader. Good for status reports and changelogs. |
| `af_sky` | Light, youthful. |
| `am_adam` | Deep male, steady. The male counterpart to `af_heart` for long reads. |
| `am_echo` | Neutral male, flatter affect. |

## British English — `--lang b`

| Voice | Character |
|---|---|
| `bf_alice` | Clear British female |
| `bf_emma` | Softer, warmer British female |
| `bm_daniel` | British male, measured |
| `bm_george` | British male, deeper |

## Other languages

| Voice | Language | `--lang` | Extra install |
|---|---|---|---|
| `ff_siwis` | French | `f` | — |
| `ef_dora` | Spanish | `e` | — |
| `if_sara` | Italian | `i` | — |
| `jf_alpha`, `jm_kumo` | Japanese | `j` | `pip install misaki[ja]` |
| `zf_xiaobei`, `zm_yunxi` | Mandarin | `z` | `pip install misaki[zh]` |

Kreyòl and Haitian French are **not** supported by Kokoro. French (`ff_siwis`,
`--lang f`) is the closest available fit for francophone content; Kreyòl text run
through any of these will be mispronounced. If Kreyòl narration matters, that
needs a different model — worth checking Chatterbox Multilingual or a fine-tune,
not solvable with a voice swap here.

## Speed

`--speed` is a multiplier applied at synthesis, so prosody stays natural rather
than sounding chipmunked.

| Speed | Feel |
|---|---|
| 1.0 | Deliberate. Good for dense specs you're actually studying. |
| 1.15 | **Default.** Natural conversational pace. |
| 1.3 | Brisk. Comfortable for familiar material. |
| 1.5+ | Skim speed. Fine for re-reads, rough on first exposure. |

## Setting a default

```bash
export MD_WHISPR_VOICE=am_adam
export MD_WHISPR_SPEED=1.3
export MD_WHISPR_LANG=a
```

Note that the cache key includes voice and speed, so switching either one
re-renders — the old audio stays cached and comes back instantly if you switch
back.
