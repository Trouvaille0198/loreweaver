/**
 * The player-facing command vocabulary used by the game input.
 *
 * Command words are protocol identifiers, not localized prose. The server
 * remains authoritative for dispatch and permissions; this module only helps
 * a player discover the common input surface before sending it.
 */
export interface CommandEntry {
  word: string
  example?: string
  keeperOnly?: boolean
}

export const COMMANDS: readonly CommandEntry[] = [
  { word: "r", example: "3d6" },
  { word: "roll", example: "3d6" },
  { word: "rd", example: "3d6" },
  { word: "rh", example: "1d100" },
  { word: "ra", example: "侦查" },
  { word: "rav", example: "侦查" },
  { word: "rc", example: "侦查" },
  { word: "rcv", example: "侦查" },
  { word: "check", example: "侦查" },
  { word: "attack", example: "斗殴" },
  { word: "sanity", example: "1d6/1d10" },
  { word: "sc", example: "1d6/1d10" },
  { word: "st", example: "HP-1" },
  { word: "sheet" },
  { word: "pc", example: "list" },
  { word: "roster", example: "list" },
  { word: "party", example: "list" },
  { word: "rename", example: "新名字" },
  { word: "avatar", example: "gen" },
  { word: "image", example: "scene" },
  { word: "npc", example: "list", keeperOnly: true },
  { word: "companion", example: "list", keeperOnly: true },
  { word: "bind", keeperOnly: true },
  { word: "unbind", keeperOnly: true },
  { word: "jrrp" },
  { word: "luck" },
  { word: "draw" },
  { word: "init" },
  { word: "ri" },
  { word: "initiative" },
  { word: "recap" },
  { word: "report", example: "detailed" },
  { word: "help" },
  { word: "h" },
  { word: "language", keeperOnly: true },
  { word: "genchar" },
  { word: "coc" },
  { word: "coc7" },
  { word: "dnd" },
  { word: "dnd5e" },
  { word: "var", example: "list", keeperOnly: true },
  { word: "vars", example: "list", keeperOnly: true },
  { word: "lore", example: "list", keeperOnly: true },
  { word: "chronicle", example: "list", keeperOnly: true },
  { word: "skill", example: "list", keeperOnly: true },
  { word: "rule", example: "list", keeperOnly: true },
  { word: "module", example: "list", keeperOnly: true },
  { word: "pack", example: "list", keeperOnly: true },
  { word: "import", example: "list", keeperOnly: true },
  { word: "room", example: "show", keeperOnly: true },
  { word: "reset", example: "confirm", keeperOnly: true },
  { word: "model", example: "list", keeperOnly: true },
  { word: "dev", example: "mount", keeperOnly: true },
  { word: "audio", example: "list", keeperOnly: true },
  { word: "bgm", example: "play", keeperOnly: true },
  { word: "ambience", example: "play", keeperOnly: true },
  { word: "amb", example: "play", keeperOnly: true },
  { word: "sfx", example: "play", keeperOnly: true },
  { word: "save", keeperOnly: true },
  { word: "undo", keeperOnly: true },
  { word: "bot", keeperOnly: true },
  { word: "botlist", keeperOnly: true },
  { word: "preset", keeperOnly: true },
  { word: "panels", keeperOnly: true },
  { word: "habits", keeperOnly: true },
]

const QUICK_PLAYER_WORDS = [
  "r", "rh", "ra", "rav", "sc", "st", "pc", "recap", "help", "draw", "jrrp", "rename", "party", "genchar", "report",
]
const QUICK_KEEPER_WORDS = ["var", "skill", "room", "panels", "audio", "image", "import", "npc", "companion", "lore", "chronicle", "rule", "model", "reset", "save"]

const COMMON_SKILLS: readonly string[] = [
  "侦查", "聆听", "图书馆使用", "闪避", "攀爬", "游泳", "斗殴", "手枪", "急救", "潜行",
  "心理学", "母语", "敏捷", "力量", "体质", "外貌", "意志", "教育", "运气",
  "Spot Hidden", "Listen", "Library Use", "Dodge", "Climb", "Swim", "Fighting", "Handgun", "First Aid", "Stealth",
  "Psychology", "Own Language", "Dexterity", "Strength", "Constitution", "Appearance", "Power", "Education", "Luck",
]

const ARGUMENTS: Record<string, readonly string[]> = {
  ra: COMMON_SKILLS,
  rav: COMMON_SKILLS,
  rc: COMMON_SKILLS,
  rcv: COMMON_SKILLS,
  check: COMMON_SKILLS,
  attack: COMMON_SKILLS,
  st: ["HP-1", "HP+1", "理智-1", "理智+1", "finalize"],
  sheet: ["HP-1", "HP+1", "理智-1", "理智+1", "finalize"],
  pc: ["list", "claim", "release"],
  roster: ["list", "claim", "release"],
  party: ["add", "new", "recruit", "act", "go", "auto", "remove", "list"],
  npc: ["list", "show", "delete"],
  companion: ["list", "delete"],
  avatar: ["gen", "generate", "clear"],
  image: ["scene", "portrait", "clue", "combat", "last"],
  report: ["detailed", "full", "log"],
}

const DICE_COMMANDS = new Set(["r", "roll", "rd", "rh"])
const SANITY_COMMANDS = new Set(["sanity", "sc"])

export interface InputSuggestion {
  text: string
  mode: "replace" | "append"
}

export interface CompletionHint {
  display: string
  next: string
  example?: string
}

function diceSuggestions(token: string): InputSuggestion[] {
  if (/^\d+$/.test(token)) return [{ text: "d", mode: "append" }]
  if (/^\d+d\d+$/i.test(token)) {
    return ["kh", "kl", "+", "-"].map((text) => ({ text, mode: "append" as const }))
  }
  return []
}

function sanitySuggestions(token: string): InputSuggestion[] {
  const slash = token.indexOf("/")
  if (slash < 0) {
    if (/^\d+$/.test(token)) return ["d", "/"].map((text) => ({ text, mode: "append" as const }))
    if (/^\d+d\d+$/i.test(token)) return [{ text: "/", mode: "append" }]
    return []
  }
  const right = token.slice(slash + 1)
  return /^\d+$/.test(right) ? [{ text: "d", mode: "append" }] : []
}

function argumentSuggestions(word: string, token: string, imageNames?: { npcs?: string[]; clues?: string[] }): InputSuggestion[] {
  if (DICE_COMMANDS.has(word)) return diceSuggestions(token)
  if (SANITY_COMMANDS.has(word)) return sanitySuggestions(token)

  const prefix = token.trim().toLowerCase()
  let candidates = [...(ARGUMENTS[word] ?? [])]
  if (word === "image") candidates = [...candidates, ...(imageNames?.npcs ?? []), ...(imageNames?.clues ?? [])]
  return candidates
    .filter((candidate, index, all) => all.indexOf(candidate) === index)
    .filter((candidate) => prefix.length === 0 || candidate.toLowerCase().startsWith(prefix))
    .slice(0, 8)
    .map((text) => ({ text, mode: "replace" as const }))
}

function visibleCommands(isKeeper: boolean): readonly CommandEntry[] {
  return COMMANDS.filter((entry) => isKeeper || !entry.keeperOnly)
}

export function commandHints(text: string, isKeeper: boolean, imageNames?: { npcs?: string[]; clues?: string[] }): CompletionHint[] {
  const prefix = text[0]
  if (prefix !== "." && prefix !== "。" && prefix !== "/") return []
  const body = text.slice(1)
  const spaceAt = body.indexOf(" ")
  if (spaceAt < 0) {
    const wordPrefix = body.toLowerCase()
    if (!wordPrefix) return []
    return visibleCommands(isKeeper)
      .filter((entry) => entry.word.startsWith(wordPrefix))
      .slice(0, 8)
      .map((entry) => ({ display: `${prefix}${entry.word}`, next: `${prefix}${entry.word} `, example: entry.example }))
  }

  const word = body.slice(0, spaceAt).toLowerCase()
  const typed = body.slice(spaceAt + 1)
  const tokenStart = typed.lastIndexOf(" ") + 1
  const token = typed.slice(tokenStart)
  const before = text.slice(0, text.length - token.length)
  return argumentSuggestions(word, token, imageNames).map((suggestion) => ({
    display: suggestion.text,
    next: suggestion.mode === "append" ? `${before}${token}${suggestion.text}` : `${before}${suggestion.text} `,
  }))
}

export function quickCommandHints(isKeeper: boolean): CompletionHint[] {
  const lookup = new Map(COMMANDS.map((entry) => [entry.word, entry]))
  const words = isKeeper ? [...QUICK_PLAYER_WORDS, ...QUICK_KEEPER_WORDS] : QUICK_PLAYER_WORDS
  return words
    .map((word) => lookup.get(word))
    .filter((entry): entry is CommandEntry => Boolean(entry) && (isKeeper || !entry.keeperOnly))
    .map((entry) => ({ display: `.${entry.word}`, next: `.${entry.word} `, example: entry.example }))
}
