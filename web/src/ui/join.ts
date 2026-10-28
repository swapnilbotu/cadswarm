// The first screen: one quiet way in. Scan the invite QR the host shows, or paste the code a
// teammate sent. No accounts, no demo; the app only opens once a build is joined.
import { esc } from './esc';

export interface JoinOptions {
  /** The build this browser opened last, offered as a quiet row under the form. */
  last: { db: string } | null;
  /** Text already tried (say, an invite that could not be reached), so a retry does not lose it. */
  code?: string;
  /** A message to show under the form before the person types anything. */
  error?: string;
  /** Join with what was scanned or pasted. Rejects with a plain-language Error to show on this screen. */
  onCode(text: string): Promise<void>;
  /** Open the camera. */
  onScan(): void;
  /** "Use another build": forget the remembered one. */
  onSwitch(): void;
}

export interface JoinScreen {
  /** Disable the form with a short status ("Joining…"), or clear it. */
  setBusy(text?: string): void;
  /** Show a plain-language problem under the form. */
  setError(text: string): void;
}

export function mountJoin(app: HTMLElement, opts: JoinOptions): JoinScreen {
  app.innerHTML = `
    <section class="join">
      <header class="join-head">
        <svg class="join-mark" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 2.8 20 7.4v9.2L12 21.2 4 16.6V7.4Z"/>
          <path d="M12 12 20 7.4M12 12 4 7.4M12 12v9.2" opacity=".45"/>
          <circle cx="12" cy="12" r="2.2" style="fill: var(--own-1)"/>
          <circle cx="17" cy="9.2" r="1.5" style="fill: var(--own-2)"/>
          <circle cx="7" cy="9.2" r="1.5" style="fill: var(--own-3)"/>
        </svg>
        <span class="join-brand">CADSwarm</span>
      </header>
      <div class="join-body">
        <h1>Open your team’s build</h1>
        <p class="join-lede">The host runs CADSwarm in FreeCAD. Scan the invite QR it shows, or paste the code a
          teammate sent you.</p>
        <button type="button" class="join-scan" data-scan>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8V5.5A1.5 1.5 0 0 1 5.5 4H8M16 4h2.5A1.5 1.5 0 0 1 20 5.5V8M20 16v2.5a1.5 1.5 0 0 1-1.5 1.5H16M8 20H5.5A1.5 1.5 0 0 1 4 18.5V16"/><path d="M8 12h8"/></svg>
          Scan invite QR
        </button>
        <p class="join-or"><span>or paste the code</span></p>
        <form class="join-form" data-form novalidate>
          <label class="sr-only" for="join-code">Invite code or phone link</label>
          <textarea id="join-code" name="code" rows="3" required autocomplete="off" autocapitalize="off"
                    spellcheck="false" placeholder="cadswarm-2:…">${esc(opts.code ?? '')}</textarea>
          <button type="submit" class="join-go">Continue</button>
          <p class="join-error" role="alert">${esc(opts.error ?? '')}</p>
        </form>
        <p class="join-fine">The code only works while the host computer is on and CADSwarm is running there.</p>
      </div>
      ${opts.last ? `<footer class="join-foot">
        <span class="join-last-label">Last build</span>
        <b class="join-last-db">${esc(opts.last.db)}</b>
        <button type="button" class="join-switch" data-switch>Use another build</button>
      </footer>` : '<footer class="join-foot"></footer>'}
    </section>`;

  const form = app.querySelector<HTMLFormElement>('[data-form]')!;
  const code = app.querySelector<HTMLTextAreaElement>('[data-code], #join-code')!;
  const go = app.querySelector<HTMLButtonElement>('.join-go')!;
  const error = app.querySelector<HTMLParagraphElement>('.join-error')!;
  const scan = app.querySelector<HTMLButtonElement>('[data-scan]')!;
  const busy = () => go.disabled;

  const screen: JoinScreen = {
    setBusy(text) {
      go.disabled = !!text;
      scan.disabled = !!text;
      code.readOnly = !!text;
      go.textContent = text || 'Continue';
    },
    setError(text) {
      error.textContent = text;
      error.hidden = !text;
    },
  };

  async function join(text: string) {
    if (busy()) return;
    screen.setError('');
    screen.setBusy('Joining…');
    try {
      await opts.onCode(text);
      // Success replaces this screen; nothing to reset.
    } catch (e) {
      screen.setBusy();
      screen.setError((e as Error)?.message ?? String(e));
      code.focus();
      code.select();
    }
  }

  form.addEventListener('submit', e => {
    e.preventDefault();
    const text = code.value.trim();
    if (!text) {
      screen.setError('Paste the invite code, or scan the QR.');
      code.focus();
      return;
    }
    void join(text);
  });

  scan.addEventListener('click', () => { if (!busy()) opts.onScan(); });
  app.querySelector('[data-switch]')?.addEventListener('click', () => opts.onSwitch());
  screen.setError(opts.error ?? '');
  return screen;
}
