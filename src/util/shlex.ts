/** `shlex.split(s, posix=True)`.
 *
 * The command parser leans on this to find file arguments, and the quoting
 * rules matter: `grep -rn "a\|b" file.md` has to come back as three tokens
 * with the regex intact. Node has no equivalent, so this is a direct
 * transcription of CPython's `shlex` in whitespace-splitting posix mode.
 */

export class ShlexError extends Error {}

const WHITESPACE = ' \t\r\n'
const QUOTES = '\'"'
const ESCAPE = '\\'
const ESCAPED_QUOTES = '"'
/** Python's shlex calls this state 'a': inside a word, not inside quotes. */
const IN_WORD = 'a'

export function split(text: string): string[] {
  const tokens: string[] = []
  let token: string | null = null
  // null = between tokens, IN_WORD = inside an unquoted token, a quote
  // character = inside those quotes, ESCAPE = the next character is escaped.
  // IN_WORD is a distinct marker rather than "" because `"'\"".includes("")`
  // is true, which would make an empty marker read as "inside quotes".
  let state: string | null = null
  let escapedState: string | null = null

  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!

    if (state === ESCAPE) {
      // Only the quote itself and the escape character are consumed inside
      // double quotes; anything else keeps its backslash.
      if (
        escapedState !== null &&
        QUOTES.includes(escapedState) &&
        ch !== ESCAPE &&
        ch !== escapedState
      ) {
        token = (token ?? '') + ESCAPE
      }
      token = (token ?? '') + ch
      state = escapedState
      escapedState = null
      continue
    }

    if (state !== null && state !== IN_WORD && QUOTES.includes(state)) {
      if (ch === state) {
        state = IN_WORD
      } else if (ch === ESCAPE && ESCAPED_QUOTES.includes(state)) {
        escapedState = state
        state = ESCAPE
      } else {
        token = (token ?? '') + ch
      }
      continue
    }

    // Between tokens, or inside an unquoted one.
    if (WHITESPACE.includes(ch)) {
      if (token !== null) {
        tokens.push(token)
        token = null
      }
      state = null
      continue
    }
    if (QUOTES.includes(ch)) {
      if (token === null) token = ''
      state = ch
      continue
    }
    if (ch === ESCAPE) {
      if (token === null) token = ''
      escapedState = state === null ? IN_WORD : state
      state = ESCAPE
      continue
    }
    token = (token ?? '') + ch
    state = IN_WORD
  }

  if (state === ESCAPE) throw new ShlexError('No escaped character')
  if (state !== null && state !== IN_WORD && QUOTES.includes(state)) {
    throw new ShlexError('No closing quotation')
  }
  if (token !== null) tokens.push(token)
  return tokens
}
