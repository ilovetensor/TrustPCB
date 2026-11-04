import { useEffect, useState } from 'react';
import Icon from './Icon.jsx';

const REPO = 'ilovetensor/trustpcb';
const GITHUB = `https://github.com/${REPO}`;
const FALLBACK_VERSION = 'v0.1.0';

const INSTALL = {
  'macOS / Linux': 'curl -fsSL https://trustpcb.planary.xyz/install.sh | sh',
  Windows: 'irm https://trustpcb.planary.xyz/install.ps1 | iex',
};
const TAB_ICONS = { 'macOS / Linux': ['apple', 'linux'], Windows: ['windows'] };

const STEPS = [
  ['Upload a KiCad board', 'Zip the project or drag the folder in, and say what the board has to do. TrustPCB works on a copy and never edits your design.'],
  ['It measures, then investigates', 'KiCad rule checks and layout analyzers run first, with no AI. Then Muse ranks suspects and tests each one with simulations.'],
  ['Read findings with evidence', 'Every finding is pinned to the board and tied to the tool output behind it, and labelled Measured, Simulated, Opinion or Not tested.'],
];

const CHECKS = [
  ['KiCad rules', 'Design and electrical rule checks through kicad-cli, plus board statistics and layer images.'],
  ['Layout analyzers', 'Decoupling per supply pin, return paths over gaps, pair skew, border tracks, via count and track impedance.'],
  ['RF antenna lab', 'openEMS simulation of printed antennas: S11 across the band, a Smith chart, the pattern, the field, what-ifs and matching parts.'],
  ['Power', 'IR drop and current density along supply nets, with what-if track changes.'],
  ['Heat', 'Board temperature from the parts that dissipate, and the hottest points, simulated.'],
  ['Signal', 'Fast nets, impedance and crossings that break the return path.'],
  ['Parts and datasheets', 'Optional DigiKey and Mouser lookups. Datasheets are read with page citations.'],
  ['Version recheck', 'Upload the next revision to see what changed and whether the earlier prediction held.'],
];

const CONTRIBUTE = [
  ['Report a bug', 'Open an issue with the board zip (if you can share it) and the review ID.', `${GITHUB}/issues/new/choose`],
  ['Add a check', 'An analyzer is one Python module with tests. CONTRIBUTING.md walks you through it.', `${GITHUB}/blob/main/CONTRIBUTING.md`],
  ['Security', 'Report privately, as described in SECURITY.md.', `${GITHUB}/blob/main/SECURITY.md`],
];

function useRepoInfo() {
  const [info, setInfo] = useState({ version: FALLBACK_VERSION, stars: null });
  useEffect(() => {
    const api = `https://api.github.com/repos/${REPO}`;
    fetch(`${api}/releases/latest`).then((r) => (r.ok ? r.json() : null)).then((r) => r?.tag_name && setInfo((i) => ({ ...i, version: r.tag_name }))).catch(() => {});
    fetch(api).then((r) => (r.ok ? r.json() : null)).then((r) => typeof r?.stargazers_count === 'number' && setInfo((i) => ({ ...i, stars: r.stargazers_count }))).catch(() => {});
  }, []);
  return info;
}

function Install() {
  const tabs = Object.keys(INSTALL);
  const [tab, setTab] = useState(() => (typeof navigator !== 'undefined' && /Win/.test(navigator.platform) ? 'Windows' : tabs[0]));
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(INSTALL[tab]).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1400); }).catch(() => {});
  };
  return (
    <div className="install">
      <div className="tabs" role="tablist">
        {tabs.map((t) => (
          <button key={t} role="tab" aria-selected={t === tab} className={t === tab ? 'tab on' : 'tab'} onClick={() => setTab(t)}>{TAB_ICONS[t].map((n) => <Icon key={n} name={n} size={14} />)}{t}</button>
        ))}
      </div>
      <div className="cmd">
        <code>{INSTALL[tab]}</code>
        <button className="copy" onClick={copy}>{copied ? 'Copied' : 'Copy'}</button>
      </div>
      <p className="dim small">Needs Node.js 22+ and git. Then run <code>npm run dev</code> in <code>~/trustpcb</code> and add a Meta or Baseten key under Settings.</p>
    </div>
  );
}

export default function App() {
  const { version, stars } = useRepoInfo();
  return (
    <>
      <header className="top">
        <a className="brand" href="#"><img src="/trustpcb-logo.svg" alt="" width="34" height="26" /> TrustPCB</a>
        <nav>
          <a href="#how">How it works</a>
          <a href="#checks">Checks</a>
          <a href="#contribute">Contributing</a>
        </nav>
        <div className="right">
          <a className="pill" href={`${GITHUB}/releases`}>{version}</a>
          <a className="pill" href={GITHUB}><Icon name="github" size={14} />GitHub{stars !== null ? ` ★ ${stars.toLocaleString()}` : ''}</a>
        </div>
      </header>

      <main>
        <section className="hero">
          <img className="hero-logo" src="/trustpcb-logo.svg" alt="" width="120" height="91" />
          <h1>Catch board problems before you pay to build them.</h1>
          <p className="lead">An open-source AI reviewer for KiCad boards. It runs real checks and simulations, and shows the evidence for every claim.</p>
          <div className="cta">
            <a className="btn primary" href={GITHUB}><Icon name="github" size={17} />View on GitHub</a>
            <a className="btn" href="#demo">Watch the demo</a>
          </div>
          <Install />
        </section>

        <section id="demo" className="demo">
          <video src="/demo.mp4" poster="/demo-poster.jpg" controls playsInline preload="none" />
          <p className="dim small">A full review of the sample nRF52840 board, running on Muse Spark Open.</p>
        </section>

        <section id="how">
          <h2>How it works</h2>
          <ol className="steps">
            {STEPS.map(([title, text], i) => (
              <li key={title} className="card"><span className="num">{i + 1}</span><h3>{title}</h3><p>{text}</p></li>
            ))}
          </ol>
        </section>

        <section id="checks">
          <h2>What it checks</h2>
          <div className="grid">
            {CHECKS.map(([title, text]) => <div key={title} className="card"><h3>{title}</h3><p>{text}</p></div>)}
          </div>
          <p className="dim small">Simulated is never called measured. If a what-if can't be built on the board, it is refused, not drawn.</p>
        </section>

        <section className="model card">
          <img src="/meta-logo.svg" alt="Meta" width="40" height="40" />
          <div>
            <h3>Powered by Meta Muse Spark</h3>
            <p>Muse is the investigating agent. It reads board images, calls tools, reasons and streams its trail. Bring a Meta API key (<code>muse-spark-1.3</code>) or a Baseten key (<code>muse-spark-open-1026</code>). The demo ran on Muse Spark Open via Baseten.</p>
          </div>
        </section>

        <section id="contribute">
          <h2>Contributing</h2>
          <div className="grid three">
            {CONTRIBUTE.map(([title, text, href]) => (
              <a key={title} className="card link" href={href}><h3>{title}</h3><p>{text}</p></a>
            ))}
          </div>
          <p className="dim small">TrustPCB is MIT licensed. Releases are cut automatically on every merge to main; see the <a href={`${GITHUB}/blob/main/CHANGELOG.md`}>changelog</a>.</p>
        </section>
      </main>

      <footer>
        <span className="iconlink">Made by <a className="iconlink" href="https://x.com/r0vrrr"><Icon name="x" size={13} /> / r0vrrr</a></span>
        <a className="iconlink" href={GITHUB}><Icon name="github" size={14} />GitHub</a>
      </footer>
    </>
  );
}
