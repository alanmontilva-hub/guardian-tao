"""Diagnóstico del latido del bot (solo horas, tamaños y versiones; nada sensible: el repo es público)."""
import json, os, time, datetime as dt, requests
S = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
EST, MAIN = os.environ["LATIDO_TOPIC"], os.environ["NTFY_TOPIC"]
CCS = dt.timezone(dt.timedelta(hours=-4))
def h(ms): return dt.datetime.fromtimestamp(ms / 1000, CCS).strftime("%d/%m %H:%M")
def leer(topic, desde="12h"):
    r = requests.get(f"{S}/{topic}/json", params={"poll": "1", "since": desde}, timeout=30)
    return [json.loads(l) for l in r.text.splitlines() if l.strip()], r.status_code
L = ["# Diagnóstico del latido del bot", f"Generado: {h(time.time()*1000)} (Caracas)", ""]
try:
    a = requests.get(f"{S}/v1/account", timeout=20).json()
    L += ["## Límites de ntfy.sh para un visitante anónimo (visto desde GitHub)", "```", json.dumps({k: a.get(k) for k in ("tier", "limits")}, indent=1), "```", ""]
except Exception as e:
    L += [f"(no pude leer /v1/account: {e})", ""]
L += ["## Configuración del guardián", "",
      f"- LATIDO_TOPIC es igual a NTFY_TOPIC: **{EST == MAIN}**",
      f"- LATIDO_TOPIC termina en _estado: **{EST.endswith('_estado')}**",
      f"- LATIDO_TOPIC es igual a NTFY_TOPIC + '_estado': **{EST == MAIN + '_estado'}**", ""]
real, codr = leer(MAIN + "_estado")
rm = [m for m in real if m.get("event") == "message"]
L += [f"## Canal REAL del bot (NTFY_TOPIC + '_estado'): {len(rm)} mensajes en 12 h (HTTP {codr})", ""]
pr, hu, vers = None, [], {}
for m in rm:
    t = m["time"] * 1000
    try:
        e = json.loads(m.get("message") or ""); vers.setdefault((e.get("v"), e.get("inst")), [t, t, e.get("ini")]); vers[(e.get("v"), e.get("inst"))][1] = t
    except Exception: pass
    if pr and (t - pr) / 60000 > 12: hu.append((pr, t, (t - pr) / 60000))
    pr = t
if pr and (time.time() * 1000 - pr) / 60000 > 12: hu.append((pr, time.time() * 1000, (time.time() * 1000 - pr) / 60000))
L += ["### Huecos de más de 12 min en el canal REAL", ""] + ([f"- {h(a)} → {h(b)}: **{g:.0f} min**" for a, b, g in hu] or ["- ninguno: el bot publicó sin cortes"]) + [""]
L += ["### Copias del bot en el canal REAL", "", "| versión | instancia | arrancó | primer estado | último estado |", "|---|---|---|---|---|"]
for (v, i), (t0, t1, ini) in vers.items():
    L.append(f"| {v} | {i} | {h(ini) if ini else '?'} | {h(t0)} | {h(t1)} |")
if rm:
    tr = [len((m.get('message') or '').encode()) for m in rm]
    L += ["", f"Tamaño del estado REAL: mín {min(tr)} · máx {max(tr)} bytes · con adjunto: {sum('attachment' in m for m in rm)}",
          f"Mensajes por hora: {len(rm) / 12:.1f} (≈ {len(rm) * 2} por día; el límite de ntfy.sh es 250 por IP)", ""]
# ¿qué campos cambian entre estados seguidos? (solo nombres de campo y conteos)
import collections
cambios = collections.Counter(); prevj = None; difs_t = []
def plano(d, pre=""):
    out = {}
    for k, v in (d.items() if isinstance(d, dict) else []):
        if isinstance(v, dict): out.update(plano(v, pre + k + "."))
        else: out[pre + k] = json.dumps(v, sort_keys=True)
    return out
for m in rm:
    try: e = plano(json.loads(m.get("message") or ""))
    except Exception: continue
    if prevj is not None:
        ks = [k for k in set(e) | set(prevj) if e.get(k) != prevj.get(k)]
        cambios[tuple(sorted(ks)) or ("(nada: latido)",)] += 1
    prevj = e
L += ["### Qué cambia entre un estado y el siguiente (canal REAL)", ""] + [f"- {n} veces: {', '.join(k)}" for k, n in cambios.most_common(12)] + [""]
est, code = leer(EST)
msgs = [m for m in est if m.get("event") == "message"]
L += [f"## Canal de estado: {len(msgs)} mensajes en 12 h (HTTP {code})", ""]
prev, insts, huecos = None, {}, []
filas = []
for m in msgs:
    t = m["time"] * 1000
    adj = "attachment" in m
    txt = m.get("message") or ""
    try:
        e = json.loads(txt); v, inst, ini = e.get("v"), e.get("inst"), e.get("ini"); legible = True
    except Exception:
        v = inst = ini = None; legible = False
    insts.setdefault(inst, [t, t, v, ini]); insts[inst][1] = t
    gap = (t - prev) / 60000 if prev else 0
    if gap > 12: huecos.append((prev, t, gap))
    filas.append(f"| {h(t)} | {gap:5.1f} | {len(txt.encode())} | {'SÍ' if adj else ''} | {'' if legible else 'NO'} | {v} | {inst} |")
    prev = t
ahora = time.time() * 1000
if prev: 
    g = (ahora - prev) / 60000
    if g > 12: huecos.append((prev, ahora, g))
L += ["### Huecos de más de 12 min sin estado", ""] + ([f"- {h(a)} → {h(b)}: **{g:.0f} min**" for a, b, g in huecos] or ["- ninguno"]) + [""]
L += ["### Copias del bot (instancias) que publicaron", "", "| instancia | versión | arrancó | primer estado | último estado |", "|---|---|---|---|---|"]
for i, (t0, t1, v, ini) in insts.items():
    L.append(f"| {i} | {v} | {h(ini) if ini else '?'} | {h(t0)} | {h(t1)} |")
tam = [len((m.get('message') or '').encode()) for m in msgs]
L += ["", f"Tamaño del estado: máx {max(tam) if tam else 0} bytes · con adjunto: {sum('attachment' in m for m in msgs)} · ilegibles: {sum(1 for f in filas if '| NO |' in f)}", ""]
mm, code2 = leer(MAIN)
claves = {"guardián: no publica estado": "no publica estado", "guardián: volvió": "volvió a publicar", "app/bot: se apaga (otra copia)": "se apaga",
          "bot iniciado": "iniciado", "error/falla": "🚨"}
L += [f"## Canal principal: {len([m for m in mm if m.get('event')=='message'])} mensajes en 12 h", ""]
for nom, k in claves.items():
    ts = [m["time"] * 1000 for m in mm if m.get("event") == "message" and k in ((m.get("title") or "") + " " + (m.get("message") or ""))]
    L.append(f"- {nom}: {len(ts)}" + (f" → {', '.join(h(t) for t in ts[-8:])}" if ts else ""))
L += ["", "## Detalle del canal de estado", "", "| hora | min desde el anterior | bytes | adjunto | ilegible | versión | instancia |", "|---|---|---|---|---|---|---|"] + filas[-150:]
os.makedirs("resultados", exist_ok=True)
open("resultados/DIAG_LATIDO.md", "w").write("\n".join(L) + "\n")
print("\n".join(L[:60]))
