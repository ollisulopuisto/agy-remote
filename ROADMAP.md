# 🗺️ agy-remote & Ekosysteemin Toteutus-Roadmap

> **Tarkka, vaiheistettu toteutussuunnitelma `agy-remote` -projektille ja sisarhankkeille (`opencode`).**  
> Tämä dokumentti on suunniteltu siten, että sen voi antaa suoraan toteuttavalle tekoälyagentille. Tehtävät on jaettu selkeisiin osakokonaisuuksiin ja merkitty vaadittavan mallitason mukaan.

---

## 🏷️ Malliluokitukset (Model Requirements)

* 🟢 **KAIKKI MALLIT (Perusmallit / Flash-Lite / GPT-4o-mini jne.)**:  
  Mekaaniset, eristetyt ja deterministiset tehtävät. Sisältää tiedostojen vendoroinnin, suoraviivaiset HTML/CSS-muutokset, yksittäiset regex-apufunktiot, selkeät Pydantic-mallit ja rutiininomaiset yksikkötestit.
* 🟡 **VAATII GEMINI 3.8 FLASHIN (tai vastaavan päättelykykyisen mallin)**:  
  Monimutkainen asynkroninen tila, Web Crypto AES-256-GCM -salauksen ja sovellustilan synkronointi, PTY/tmux-prosessien elinkaari ja signaalit (SIGINT/SIGKILL), kilpajuoksuttomat tilasiirtymät ja hook-arkkitehtuuri.

---

## 🧭 Arkkitehtoniset Reunaehdot (Älä riko näitä!)

1. **Ei ulkopuolisia CDN-riippuvuuksia:** Tiukka CSP (`script-src 'self'`). Kaikki kirjastot vendroidaan paikallisesti `src/agy_remote/static/` -hakemistoon.
2. **Päästä päähän -salaus (E2EE):** Kaikki WebSocket-sanomat selaimen ja palvelimen välillä on suojattu AES-256-GCM -salauksella. Selain purkaa salauksen muistissa URL-tiivisteen (`#key=...`) avulla.
3. **Komentorivityökalut & Testaus:**
   * Paketinhallinta: `uv`
   * Testit: `uv run pytest`
   * Tyylitarkistus ja formatointi: `uv run ruff check .` ja `uv run ruff format .` (ajettava ennen jokaista committia!)
   * Versiointi: CalVer (`vYY.MM.DD.N`), jossa `N` on commit-laskuri.

---

## 📋 VAIHE 1: Granulaariset Työkaluhyväksynnät (Granular Tool Approvals)

Tavoite: Poistaa mobiilikäytön hyväksyntäuupumus sallimalla lukuoperaatioiden (`view_file`, `grep_search`, `list_dir`) automaattihyväksyntä turvallisesti, vaatien silti kuittauksen muokkaus- ja ajotoiminnoille (`run_command`, tiedostojen ylikirjoitus, git push).

### Tehtävä 1.1: Luku- ja tarkastelutyökalujen luokittelu
* **Tila:** ✅ **VALMIS (v26.09.05.114)**
* **Taso:** 🟢 KAIKKI MALLIT
* **Kohdetiedostot:**
  * `src/agy_remote/hooks.py`
  * `src/agy_remote/static/format.js`
* **Mitä tehtiin:**
  - Määriteltiin `READ_ONLY_TOOLS` ja `is_read_only_tool()` luokittelemaan luku- ja hakukomennot.
  - Varmistettu, ettei `ask_question`-työkalua koskaan luokitella automaattisesti hyväksyttäväksi.

### Tehtävä 1.2: Serverin hyväksyntäreitittimen logiikka
* **Tila:** ✅ **VALMIS (v26.09.05.114)**
* **Taso:** 🟡 VAATII GEMINI 3.8 FLASHIN
* **Kohdetiedostot:**
  * `src/agy_remote/server.py`
  * `src/agy_remote/session_manager.py`
* **Mitä tehtiin:**
  - Lisätty kolmitilainen `approval_policy` (`ask_all`, `auto_reads`, `auto_all`) `SessionManageriin`.
  - `request_approval()` palauttaa luvallisen hyväksynnän suoraan ilman viivettä tai mobiilibanneria, kun politiikka sallii työkalun.
  - Lisätty REST-reitit `GET /api/approvals/policy` ja `POST /api/approvals/policy` sekä WebSocket-sanomanhallinta.
  - Verifioitu `tests/test_approval_policy.py`-testisarjalla.

### Tehtävä 1.3: Mobiilikäyttöliittymän 3-asentoinen valitsin
* **Taso:** 🟢 KAIKKI MALLIT
* **Kohdetiedostot:**
  * `src/agy_remote/static/index.html`
  * `src/agy_remote/static/style.css`
* **Mitä tehdään:**
  1. Päivitä yläpalkin tai drawerin `autoAcceptBtn` näyttämään kolme selkeää tilaa:
     - 🔴 *Kysy kaikki* (Kilpi lukittuna / oletus)
     - 🟡 *Salli luku* (Kilpi + kirjan/silmän kuvake)
     - 🟢 *Salli kaikki* (Kilpi avoinna)
  2. Lisää tilojen CSS-korostusvärit ja selkeät aria-label/title-tekstit.
* **Verifiointi:** Painikkeen ulkoasun ja tilavaihdon tarkistus selaimessa mobiilinäkymässä.

### Tehtävä 1.4: Client-puolen tilanvaihto ja synkronointi
* **Taso:** 🟢 KAIKKI MALLIT
* **Kohdetiedostot:**
  * `src/agy_remote/static/app.js`
* **Mitä tehdään:**
  1. Muuta klikkaustapahtumankäsittelijä kiertämään tilat: `ask_all` -> `auto_reads` -> `auto_all` -> `ask_all`.
  2. Tallenna valinta `localStorage`-muistiin sessiokohtaisesti tai globaalisti.
  3. Lähetä salattu WebSocket-sanoma palvelimelle: `{ type: "set_approval_policy", policy: currentPolicy }`.
* **Verifiointi:** Selaimen reload säilyttää valitun profiilin ja palvelin kuittaa asetuksen.

---

## 📋 VAIHE 2: Alpine.js -käyttöliittymämigraatio (PWA UI Shell)

Tavoite: Korvata `src/agy_remote/static/app.js`:n ~130 KB imperatiivinen DOM-koodi deklaratiivisilla Alpine.js-komponenteilla ilman erillistä build-steppiä tai npm-riippuvuuksia.

### Tehtävä 2.1: Alpine.js:n vendorointi ja CSP-varmennus
* **Taso:** 🟢 KAIKKI MALLIT
* **Kohdetiedostot:**
  * `src/agy_remote/static/alpine.min.js` (uusi tiedosto)
  * `src/agy_remote/static/index.html`
* **Mitä tehdään:**
  1. Tallenna virallinen Alpine.js v3.x minified-jakelu tiedostoon `src/agy_remote/static/alpine.min.js`.
  2. Lisää skripti `index.html`:n `<head>`-osaan:
     ```html
     <script defer src="/static/alpine.min.js"></script>
     ```
  3. Varmista, että `index.html`:n CSP-määritys (`script-src 'self'`) sallii skriptin suorituksen ilman virheitä.
* **Verifiointi:** Avaa sivu ja tarkista selaimen konsoli (ei CSP-virheitä, `window.Alpine` on määritelty).

### Tehtävä 2.2: Sivupalkin (Drawer) ja välilehtien deklaratiivinen refaktorointi
* **Taso:** 🟢 KAIKKI MALLIT
* **Kohdetiedostot:**
  * `src/agy_remote/static/index.html`
  * `src/agy_remote/static/app.js`
* **Mitä tehdään:**
  1. Muuta drawer `index.html`:ssä käyttämään Alpine-direktiivejä:
     ```html
     <div x-data="{ open: false, tab: 'agents' }" @toggle-drawer.window="open = !open">
       <div x-show="open" x-transition.opacity class="drawer-backdrop" @click="open = false"></div>
       <div x-show="open" x-transition:enter="..." class="drawer">
         ...
         <button :class="{ active: tab === 'agents' }" @click="tab = 'agents'">Kaikki agentit</button>
         <button :class="{ active: tab === 'sessions' }" @click="tab = 'sessions'">Sessiot</button>
       </div>
     </div>
     ```
  2. Poista `app.js`:stä manuaaliset `document.getElementById('drawer').classList.add('open')` jne.
* **Verifiointi:** Drawer avautuu ja sulkeutuu sulavasti animaatiolla, ja välilehdet vaihtuvat ilman `app.js`:n DOM-manipulaatiota.

### Tehtävä 2.3: Modaalidialogien (New Session, Meta Task) muunto
* **Taso:** 🟢 KAIKKI MALLIT
* **Kohdetiedostot:**
  * `src/agy_remote/static/index.html`
  * `src/agy_remote/static/app.js`
* **Mitä tehdään:**
  1. Muunna New Session Sheet ja Meta-AGY Task Sheet Alpine-komponenteiksi (`x-data="{ show: false, repo: '', branch: '' }"`).
  2. Korvaa suorat `hidden`-attribuuttien kytkennät `x-show`- ja `x-transition`-määrityksillä.
  3. Siisti `app.js`:stä pois lomakekenttien manuaalinen tyhjennyskoodi ja sido ne `x-model`-sidoksilla.
* **Verifiointi:** Lomakkeet avautuvat, validaatiot toimivat ja sulkeminen tyhjentää tilan oikein.

### Tehtävä 2.4: E2EE WebSocket -tapahtumien ja Alpine Store -integraatio
* **Tila:** ✅ **VALMIS (v26.09.05.114)**
* **Taso:** 🟡 VAATII GEMINI 3.8 FLASHIN
* **Kohdetiedostot:**
  * `src/agy_remote/static/app.js`
* **Mitä tehtiin:**
  - Alustettu `Alpine.store('agy', ...)` ja `CustomEvent('agy:event')` -tapahtumaväylä.
  - Säilytetty Web Crypto AES-256-GCM -salaus ja liitetty puretut viestit suoraan globaaliin tilaan.
  - Lisätty Alpine-yhteensopivat toiminnot (`setApprovalPolicy`, `sendInterrupt`, `sendKill`, `reorderPromptQueue`).

---

## 📋 VAIHE 3: Prosessin Jumiutumisvahti (Hang / Stall Watchdog)

Tavoite: Tunnistaa tilanteet, joissa CLI-ajuri tai taustakomento on jumiutunut (esim. odottaa käyttäjän syötettä PTY:ssä ilman että kyselyä on havaittu, tai verkko-operaatio on jäätynyt), ja antaa mobiilikäyttäjälle yhden kosketuksen `SIGINT` / `SIGKILL` -toiminnot.

### Tehtävä 3.1: Sydänääni- ja aktiivisuusmittari supervisor-tasolla
* **Tila:** ✅ **VALMIS (v26.09.05.114)**
* **Taso:** 🟡 VAATII GEMINI 3.8 FLASHIN
* **Kohdetiedostot:**
  * `src/agy_remote/session_manager.py`
  * `src/agy_remote/pty_runner.py`
  * `src/agy_remote/tmux_runner.py`
* **Mitä tehtiin:**
  - Lisätty `_last_output_time`-seuranta ja `note_output()` PTY- ja Tmux-lukijoihin.
  - Lisätty `check_stalled_sessions()` ja `is_session_stalled()` `SessionManageriin`.
  - Lisätty `PtySupervisor.kill()` ja `TmuxSupervisor.kill()`.
  - Verifioitu `tests/test_watchdog.py`-testisarjalla.

### Tehtävä 3.2: Tapahtumaviestit ja hätäkeskeytys-API
* **Tila:** ✅ **VALMIS (v26.09.05.114)**
* **Taso:** 🟡 VAATII GEMINI 3.8 FLASHIN
* **Kohdetiedostot:**
  * `src/agy_remote/server.py`
  * `src/agy_remote/session_manager.py`
* **Mitä tehtiin:**
  - Lisätty `interrupt_session()` ja `kill_session()` `SessionManageriin`.
  - Lisätty `POST /api/sessions/{session_id}/interrupt` ja `POST /api/sessions/{session_id}/kill` REST-reitit.
  - Lisätty WebSocket-viestikäsittelijät `interrupt_session` ja `kill_session`.
  - Verifioitu `tests/test_watchdog.py`-testisarjalla.

### Tehtävä 3.3: Mobiilikortti jumiutumiselle
* **Taso:** 🟢 KAIKKI MALLIT
* **Kohdetiedostot:**
  * `src/agy_remote/static/index.html`
  * `src/agy_remote/static/style.css`
  * `src/agy_remote/static/app.js`
* **Mitä tehdään:**
  1. Rakenna varoituskortti syötteen yläpuolelle, joka ilmestyy vain kun `session.stalled === true` (tai kun kuunnellaan `agy:event` stalled-tapahtumaa).
  2. Tarjoa kaksi toimintopainiketta:
     - `[Keskeytä (Ctrl+C)]` (oranssi painike -> kutsuu `Alpine.store('agy').sendInterrupt()`)
     - `[Pakkotapa]` (punainen painike -> kutsuu `Alpine.store('agy').sendKill()`)
  3. Kytke painikkeet kutsumaan vastaavia keskeytysreittejä ja anna mobiilikäyttäjälle haptinen palaute (`navigator.vibrate([40, 60, 40])`).
* **Verifiointi:** Tarkista, että kortti ilmestyy emuloidulla tapahtumalla ja poistuu heti kun tulostevirta jatkuu.

---

## 📋 VAIHE 4: Mobiilijonotus & Useamman Promptin Backlog

Tavoite: Mahdollistaa useamman tehtävän syöttäminen jonoon puhelimelta siten, että agentti siirtyy automaattisesti seuraavaan tehtävään edellisen valmistuttua.

### Tehtävä 4.1: Jonotietorakenne ja käsittely
* **Tila:** ✅ **VALMIS (v26.09.05.114)**
* **Taso:** 🟢 KAIKKI MALLIT
* **Kohdetiedostot:**
  * `src/agy_remote/session_manager.py`
  * `src/agy_remote/models.py`
* **Mitä tehtiin:**
  - Jonotietorakenne, `enqueue_prompt`, `dequeue_prompt`, `remove_queued_prompt` ja `reorder_queued_prompts()` toteutettu ja testattu.

### Tehtävä 4.2: Automaattinen vuoronsiirto turnin päättyessä
* **Tila:** ✅ **VALMIS (v26.09.05.114)**
* **Taso:** 🟡 VAATII GEMINI 3.8 FLASHIN
* **Kohdetiedostot:**
  * `src/agy_remote/session_manager.py`
  * `src/agy_remote/pty_runner.py`
* **Mitä tehtiin:**
  - `_drain_queue_if_idle()` purkaa jonon automaattisesti, kun sessio vapautuu idle-tilaan.
  - REST- ja WS-reitit jonon uudelleenjärjestelylle lisätty (`/api/sessions/{id}/queue/reorder`).

### Tehtävä 4.3: Käyttöliittymäkomponentti jonon hallintaan
* **Taso:** 🟢 KAIKKI MALLIT
* **Kohdetiedostot:**
  * `src/agy_remote/static/index.html`
  * `src/agy_remote/static/app.js`
* **Mitä tehdään:**
  1. Lisää syöttökentän viereen pieni merkki: "Jonossa (N)".
  2. Luo ponnahdusikkuna tai lista, josta käyttäjä voi poistaa jonotetun viestin tai muokata sen tekstiä ennen kuin se lähetetään.
* **Verifiointi:** Syötä viesti agentin ollessa varattuna -> viesti siirtyy jonoon näkyviin -> suorituksen päätyttyä viesti poistuu jonosta ja syötetään istuntoon.

---

## 🚫 Hylätyt / Poissuljetut Osa-alueet (Älä toteuta näitä)

* **HTMX 4 E2EE-pääsyötteessä:** Hylätty arkkitehtuuriristiriidan vuoksi. HTMX odottaa palvelimen palauttavan suoraa HTML-tekstiä HTTP-pyynnöllä, mikä rikkoo selaimessa purettavan salauskerroksen.
* **HTMX `opencode`-projektissa:** Hylätty; `opencode` perustuu SolidJS:n hienojakoiseen reaktiivisuuteen, johon HTMX ei sovi.
* **Docker / MicroVM Sandboxing (`--sandbox`):** Siirretty tulevaisuuteen korkean ylläpitotaakan vuoksi (avainten, Tailscale-verkkojen ja `uv`-välimuistien läpivienti).
