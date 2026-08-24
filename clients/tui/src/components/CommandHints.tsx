import type { Palette } from "../themes"
import { tt } from "../i18n"
import type { CompletionHint } from "../commandCompletion"

export interface CommandHintsProps {
  hints: CompletionHint[]
  selectedIndex: number
  theme: Palette
  locale?: string
  quick?: boolean
}

export function CommandHints({ hints, selectedIndex, theme, locale, quick = false }: CommandHintsProps) {
  if (hints.length === 0) return null
  const pageStart = Math.min(
    Math.max(0, selectedIndex - 7),
    Math.max(0, hints.length - 8),
  )
  const visibleHints = hints.slice(pageStart, pageStart + 8)
  return (
    <box
      flexDirection="column"
      border
      borderColor={theme.border}
      paddingX={1}
      flexShrink={0}
      // Two border rows plus two padding rows sit outside the content rows.
      height={visibleHints.length + 4}
    >
      <text fg={theme.dim} wrapMode="none" truncate>
        {tt(locale, quick ? "game.quickCommands" : "game.commandHints")}
      </text>
      {visibleHints.map((hint, index) => {
        const actualIndex = pageStart + index
        return (
          <text
            key={`${hint.display}-${actualIndex}`}
            fg={actualIndex === selectedIndex ? theme.bg : theme.fg}
            bg={actualIndex === selectedIndex ? theme.accent : theme.bg}
            wrapMode="none"
            truncate
          >
            {`${actualIndex === selectedIndex ? "▸" : " "} ${hint.display}${hint.example ? `  ${hint.example}` : ""}`}
          </text>
        )
      })}
      <text fg={theme.dim} wrapMode="none" truncate>
        {tt(locale, "game.commandHintHelp")}
      </text>
    </box>
  )
}
