"""
Guardián TAO v2.7 (28-oct-2026) — corre en GitHub Actions, sin estado propio. No es asesoría financiera.

La única fuente de verdad es Bitunix:
  - hora de apertura  -> ctime de la posición
  - entrada           -> avgOpenPrice
  - stop vigente      -> orden TP/SL de la posición
  - candado hecho     -> el stop ya está del lado de la ganancia (o en entrada)

v2.7: · SIN NTFY (se agotaba el cupo diario de ntfy.sh). Los avisos van por TELEGRAM (secretos TELEGRAM_TOKEN y
        TELEGRAM_CHAT_ID, los mismos del bot en Render) y el latido del bot se lee SOLO de estado.json en GitHub (ESTADO_TOKEN).
        Un aviso por caída del bot (repite cada 3 h si sigue caído) y aviso cuando vuelve; de noche llegan sin sonido.
v2.6: · Respeta las operaciones que el bot v8.0 deja correr (stop asegurando ganancia → hasta 96 h o la salida publicada).
v2.4: · ESTADO DEL BOT DESDE GITHUB (bot v7.7+): con el secreto ESTADO_TOKEN (token de solo lectura de libro-tao) lee
        estado.json de la rama "estado" (modos de cada posición y latido_t). Sin el secreto, o si GitHub falla, usa ntfy como antes.
v2.3: · CANDADO TARDÍO (igual que el bot v7.2): a las CORTE_H (12) se cierra la posición que NO ganaba; la que ganaba sigue con su
        stop y a las CANDADO_H (14) el stop sube a asegurar el 90% (si a esa hora ya no gana, se cierra). Para saber si ganaba a
        las 12h lee de Bitunix la vela de 1 min que cierra en ese momento (sin estado propio). CANDADO_H=12 devuelve la regla v2.2.
v2.2: · MODO acepta REAL/real/Real (antes "REAL" en mayúsculas dejaba el guardián en PAPEL sin avisar).
      · Respeta el modo de cada posición que publica el bot v6.6 (app / Telegram): 👤 MANUAL = no aplica 12h ni 24h
        (solo stop de emergencia si no tiene stop y aviso de riesgo). 🤖 AUTO = reglas completas, también para
        posiciones abiertas a mano. Si no hay estado del bot, trata todo como AUTO (lo más protector).

Reglas (en cada corrida, idempotentes):
  1. Sin stop en Bitunix           -> pone stop de emergencia (4%) y avisa 🚨
  2. Riesgo abierto > RIESGO_MAX % -> avisa 🚨 (detecta entradas dobles)
  3. Órdenes de entrada del bot duplicadas, o pendientes con posición abierta -> las cancela
  4. Edad >= CORTE_H (12h) y candado no hecho: si a las 12h NO ganaba -> cierra. Edad >= CANDADO_H (14h) y candado no hecho:
     si pierde -> cierra; si gana -> stop asegura 90% (v2.1: mueve TODOS los stops, lo verifica leyendo Bitunix y, si no
     lo puede confirmar, CIERRA la posición). Con CANDADO_H<=CORTE_H hay una sola revisión (regla v2.2).
  5. Edad >= 24h                    -> cierra, SALVO (v2.6) que el stop ya asegure ganancia: el bot v8.0 deja correr
     esas operaciones con stop móvil; entonces el límite es CORRER_H (96 h) o la salida publicada por el bot (máx. 168 h).
  6. Latido del bot (v2, sin falsas alarmas):
     · avisa solo si el bot lleva más de LATIDO_MIN (por defecto 30) min sin publicar;
     · NUNCA como alarma: es una notificación normal (y silenciosa de 10 p.m. a 7 a.m. Caracas),
       porque el guardián ya protege cualquier posición abierta aunque el bot esté caído;
     · un solo aviso por caída (repite cada 3 h si sigue caído) y avisa cuando el bot vuelve.

Variables de entorno:
  BITUNIX_API_KEY, BITUNIX_SECRET_KEY   (llave SIN permiso de retiro)
  SIMBOLOS=TAOUSDT   CORTE_H=12   CANDADO_H=14   GRACIA_MIN=10   RIESGO_MAX=2.0   MODO=real|papel
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (v2.7: avisos por Telegram)   LATIDO_MIN=30
  PREFIJO_BOT=taol
  ESTADO_TOKEN=<token solo lectura>  ESTADO_REPO=alanmontilva-hub/libro-tao  ESTADO_RAMA=estado   (v2.4)
"""
import os, time, json, uuid, hashlib, requests
from datetime import datetime, timezone, timedelta

BASE = "https://fapi.bitunix.com"
H = 3600_000
HOLD_H, CANDADO = 24, 0.90
STOP_EMERGENCIA = 0.04
CARACAS = timezone(timedelta(hours=-4))
NOCHE = (22, 7)                      # de 10 p.m. a 7 a.m. Caracas: avisos de latido en silencio
TXT_CAIDO, TXT_VOLVIO = "no publica estado", "volvió a publicar"

def env(k, d=None):
    v = os.environ.get(k)
    return v if v not in (None, "") else d

CORTE_H = int(env("CORTE_H", 12))        # v2.3: a esta hora se cierra la posición que NO ganaba
CORRER_H = float(env("CORRER_H", 96))    # v2.6: límite de una operación que el bot deja correr (stop asegurando ganancia)
CANDADO_H = int(env("CANDADO_H", 14))    # v2.3: a esta hora la que gana asegura el CANDADO (v2.2: 12). Debe ser igual que en el bot

# ───────── Cliente Bitunix (firma doble SHA256 de la doc oficial) ─────────
class Bitunix:
    def __init__(self, key, secret, http=None):
        self.key, self.secret, self.http = key, secret, http or requests

    def _firma(self, nonce, ts, query, body):
        q = "".join(f"{k}{query[k]}" for k in sorted(query)) if query else ""
        d = hashlib.sha256((nonce + ts + self.key + q + body).encode()).hexdigest()
        return hashlib.sha256((d + self.secret).encode()).hexdigest()

    def _req(self, metodo, path, query=None, data=None):
        nonce, ts = uuid.uuid4().hex, str(int(time.time() * 1000))
        body = json.dumps(data, separators=(",", ":")) if data is not None else ""
        hdr = {"api-key": self.key, "nonce": nonce, "timestamp": ts, "language": "en-US",
               "Content-Type": "application/json", "sign": self._firma(nonce, ts, query or {}, body)}
        if metodo == "GET":
            r = self.http.get(BASE + path, params=query, headers=hdr, timeout=20)
        else:
            r = self.http.post(BASE + path, data=body, headers=hdr, timeout=20)
        j = r.json()
        if j.get("code") != 0:
            raise RuntimeError(f"Bitunix {path}: {j}")
        return j.get("data")

    @staticmethod
    def _lista(d, *claves):
        if isinstance(d, list):
            return d
        for k in claves:
            if isinstance(d, dict) and d.get(k):
                return d[k]
        return []

    # lectura
    def posiciones(self, sym):
        return self._lista(self._req("GET", "/api/v1/futures/position/get_pending_positions", {"symbol": sym}), "positionList")
    def tpsl(self, sym):
        return self._lista(self._req("GET", "/api/v1/futures/tpsl/get_pending_orders", {"symbol": sym, "limit": 100}), "orderList")
    def ordenes(self, sym):
        return self._lista(self._req("GET", "/api/v1/futures/trade/get_pending_orders", {"symbol": sym}), "orderList")
    def precio(self, sym):
        d = self.http.get(BASE + "/api/v1/futures/market/kline", params={"symbol": sym, "interval": "1m", "limit": 2}, timeout=20).json()["data"]
        return float(max(d, key=lambda k: int(k["time"]))["close"])
    def cierre_en(self, sym, t_ms):
        """v2.3: cierre de la última vela de 1m ya cerrada a las t_ms (pública; endTime = tope, como usa backtest_tao.py)."""
        d = self.http.get(BASE + "/api/v1/futures/market/kline",
                          params={"symbol": sym, "interval": "1m", "limit": 5, "endTime": int(t_ms)}, timeout=20).json()["data"]
        d = [k for k in d if int(k["time"]) + 60_000 <= t_ms]
        if not d:
            raise ValueError("sin vela de 1m cerrada a esa hora")
        return float(max(d, key=lambda k: int(k["time"]))["close"])
    def capital(self):
        d = self._req("GET", "/api/v1/futures/account", {"marginCoin": "USDT"})
        x = d[0] if isinstance(d, list) else d
        return sum(float(x.get(k) or 0) for k in ("available", "frozen", "margin", "crossUnrealizedPNL", "isolationUnrealizedPNL"))
    # acciones
    def cerrar(self, pid):
        return self._req("POST", "/api/v1/futures/trade/flash_close_position", data={"positionId": str(pid)})
    def poner_stop(self, sym, pid, sl, existe):
        path = "/api/v1/futures/tpsl/position/modify_order" if existe else "/api/v1/futures/tpsl/position/place_order"
        return self._req("POST", path, data={"symbol": sym, "positionId": str(pid), "slPrice": f"{sl:.2f}", "slStopType": "LAST_PRICE"})
    def modificar_sl_orden(self, oid, sl, qty=None):
        d = {"orderId": str(oid), "slPrice": f"{sl:.2f}", "slStopType": "LAST_PRICE", "slOrderType": "MARKET"}
        if qty:
            d["slQty"] = str(qty)
        return self._req("POST", "/api/v1/futures/tpsl/modify_order", data=d)
    def cancelar(self, sym, oids):
        return self._req("POST", "/api/v1/futures/trade/cancel_orders",
                         data={"symbol": sym, "orderList": [{"orderId": str(o)} for o in oids]})

# ───────── Lógica pura (sin red): decide qué hacer ─────────
def lado_de(pos):
    s = str(pos.get("side", "")).upper()
    return 1 if s in ("LONG", "BUY") else -1          # la doc dice LONG/SHORT; el historial usa BUY/SELL

def stop_de(pos, tpsl):
    """El stop MÁS protector vigente (el más alto en largos, el más bajo en cortos)."""
    sls = [float(t["slPrice"]) for t in tpsl
           if str(t.get("positionId")) == str(pos.get("positionId")) and t.get("slPrice") not in (None, "", "0")]
    if not sls:
        return None, False
    return (max(sls) if lado_de(pos) == 1 else min(sls)), True

def modo_pos(estado, pid):
    """Modo publicado por el bot (canal _estado). Sin dato → AUTO."""
    e = estado or {}
    for o in e.get("ops") or []:
        if o.get("k") == "pos" and str(o.get("id")) == str(pid):
            return o.get("modo") or "auto"
    return (e.get("modos") or {}).get(str(pid)) or e.get("modo_def") or "auto"

def estado_github(cfg, http=requests):
    """v2.4: estado.json que el bot escribe en GitHub (permanente). None si no hay token o falla."""
    if not cfg.get("estado_token"):
        return None
    try:
        r = http.get(f"https://api.github.com/repos/{cfg['estado_repo']}/contents/estado.json",
                     params={"ref": cfg["estado_rama"]}, timeout=20,
                     headers={"Authorization": f"Bearer {cfg['estado_token']}", "Accept": "application/vnd.github.raw+json"})
        if r.status_code == 200:
            return json.loads(r.text)
        print("estado GitHub:", r.status_code, r.text[:150])
    except Exception as e:
        print("estado GitHub:", e)
    return None

def ultimo_estado(cfg, http=requests):
    return estado_github(cfg, http)              # v2.7: solo GitHub (sin ntfy)

def salida_publicada(estado, pid):
    """v2.6: hora de cierre que el bot publicó para esa posición (t24 = salida; si la deja correr, más tarde)."""
    for o in (estado or {}).get("ops") or []:
        if o.get("k") == "pos" and str(o.get("id")) == str(pid) and o.get("t24"):
            return int(o["t24"])
    return None

def decidir(pos, tpsl, ordenes, precio, ahora, capital, cfg, manual=False, p12=None, salida=None):
    """Acciones: ("cerrar", motivo) | ("stop", precio, existe, motivo) | ("cancelar", [oids], motivo) | ("aviso", texto)"""
    acc = []
    lado = lado_de(pos)
    ent = float(pos.get("avgOpenPrice") or pos.get("entryPrice"))
    qty = float(pos.get("qty"))
    t0 = int(pos["ctime"])
    edad_h = (ahora - t0) / H
    gracia = cfg["gracia_min"] / 60
    sl, existe = stop_de(pos, tpsl)
    pref = cfg["prefijo"]
    entradas = [o for o in ordenes if str(o.get("clientId") or "").startswith(pref) and not o.get("reduceOnly")]
    if entradas:
        acc.append(("cancelar", [o["orderId"] for o in entradas], "orden de entrada del bot pendiente con posición ya abierta"))
    asegura = sl is not None and (sl - ent) * lado > 0
    lim_h = HOLD_H
    if asegura:                                     # v2.6: puede estar corriendo (bot v8.0) → no se corta a las 24 h
        lim_h = max(HOLD_H, CORRER_H)
        if salida:
            lim_h = min(168.0, max(lim_h, (salida - t0) / H))
    if edad_h >= lim_h + gracia and not manual:
        acc.append(("cerrar", f"{lim_h:.0f}h cumplidas ({edad_h:.1f}h)"))
        return acc
    if sl is None:
        sl = ent * (1 - STOP_EMERGENCIA * lado)
        acc.append(("stop", sl, False, "🚨 posición SIN stop en Bitunix: stop de emergencia al 4%"))
        existe = True
    riesgo = max(0.0, (ent - sl) * lado) * qty
    if capital and riesgo / capital * 100 > cfg["riesgo_max"]:
        acc.append(("aviso", f"🚨 riesgo abierto {riesgo:.2f} USDT = {riesgo / capital * 100:.1f}% del capital "
                             f"(máx {cfg['riesgo_max']}%). Posible entrada doble: qty {qty}."))
    if manual:
        return acc                                   # 👤 MANUAL: sin 12h ni 24h
    candado_hecho = (sl - ent) * lado >= 0
    if candado_hecho:
        return acc
    g = (precio - ent) * lado
    if CANDADO_H > CORTE_H and edad_h < CANDADO_H + gracia:
        # v2.3: entre el corte y el candado solo se cierra la que NO ganaba a las CORTE_H (p12 = precio de esa hora)
        if edad_h >= CORTE_H + gracia:
            if p12 is None:
                if g <= 0:
                    acc.append(("cerrar", f"revisión {CORTE_H}h: va perdiendo ({g:+.2f} por unidad) y no pude leer el precio de las {CORTE_H}h"))
            elif (p12 - ent) * lado <= 0:
                acc.append(("cerrar", f"revisión {CORTE_H}h: a las {CORTE_H}h no ganaba ({(p12 - ent) * lado:+.2f} por unidad)"))
        return acc
    if edad_h >= CANDADO_H + gracia:
        if g <= 0:
            acc.append(("cerrar", f"revisión {CANDADO_H}h: va perdiendo ({g:+.2f} por unidad)"))
        else:
            nuevo = ent + lado * CANDADO * g
            if (nuevo - sl) * lado > 0:
                acc.append(("stop", nuevo, existe, f"revisión {CANDADO_H}h: gana {g:+.2f}; stop asegura 90% en {nuevo:.2f}"))
    return acc

def precio_corte(bx, sym, pos, ahora, cfg, p12_fn=None, manual=False):
    """v2.3: precio de la hora del corte, solo si la posición está entre CORTE_H y CANDADO_H (si no, no hace falta)."""
    if manual or CANDADO_H <= CORTE_H:
        return None
    t0 = int(pos["ctime"]); edad_h = (ahora - t0) / H; gracia = cfg["gracia_min"] / 60
    if not (CORTE_H + gracia <= edad_h < CANDADO_H + gracia):
        return None
    try:
        return float((p12_fn or bx.cierre_en)(sym, t0 + CORTE_H * H))
    except Exception as e:
        print("precio de las %dh:" % CORTE_H, e)
        return None

def depurar_entradas_sin_posicion(ordenes, cfg):
    """Sin posición: si hay más de una orden de entrada del bot viva, deja solo la más nueva."""
    pref = cfg["prefijo"]
    e = sorted([o for o in ordenes if str(o.get("clientId") or "").startswith(pref) and not o.get("reduceOnly")],
               key=lambda o: int(o.get("ctime") or 0))
    if len(e) > 1:
        return [("cancelar", [o["orderId"] for o in e[:-1]], f"{len(e)} órdenes de entrada del bot vivas a la vez (entrada doble)")]
    return []

def cubierto(tpsl, pos, nivel):
    """True si los stops vigentes en `nivel` o mejor cubren toda la posición."""
    lado, qty = lado_de(pos), float(pos.get("qty"))
    c = 0.0
    for t in tpsl:
        if str(t.get("positionId")) == str(pos.get("positionId")) and t.get("slPrice") not in (None, "", "0"):
            if (float(t["slPrice"]) - nivel) * lado >= -0.01:
                c += float(t.get("slQty") or 0) or qty
    return c >= qty * 0.999

def mover_verificado(bx, sym, pos, nivel):
    """v2.1: mueve TODOS los stops (los de orden y el de posición) y verifica leyendo Bitunix."""
    for t in bx.tpsl(sym):
        if str(t.get("positionId")) == str(pos.get("positionId")) and t.get("slPrice") not in (None, "", "0"):
            try:
                bx.modificar_sl_orden(t.get("id") or t.get("orderId"), nivel, t.get("slQty"))
            except Exception as e:
                print("modificar stop de orden:", e)
    vig = bx.tpsl(sym)
    if not cubierto(vig, pos, nivel):
        existe = any(str(t.get("positionId")) == str(pos.get("positionId")) and not t.get("slQty") for t in vig)
        try:
            bx.poner_stop(sym, pos["positionId"], nivel, existe)
        except Exception as e:
            print("stop de posición:", e)
    return cubierto(bx.tpsl(sym), pos, nivel)

# ───────── Latido del bot (v2) ─────────
def es_noche(ahora_ms):
    h = datetime.fromtimestamp(ahora_ms / 1000, CARACAS).hour
    return h >= NOCHE[0] or h < NOCHE[1]

LATIDO_EST = {"caido_t": 0}          # v2.7: memoria del aviso de caída dentro de la corrida (antes se leía de ntfy)

def revisar_latido(cfg, ahora, hay_algo_abierto, http=requests):
    """Devuelve (texto, prioridad) o None. Un aviso por caída (repite cada 3 h), sin alarma, y aviso de regreso."""
    if cfg["latido_min"] <= 0 or not cfg.get("estado_token"):
        return None
    eg = estado_github(cfg, http)
    if eg is None or not eg.get("latido_t"):
        return None                                # GitHub no respondió: no se acusa al bot
    edad_min = (ahora - int(eg["latido_t"])) / 60000
    if edad_min <= cfg["latido_min"]:
        if LATIDO_EST["caido_t"]:
            LATIDO_EST["caido_t"] = 0
            return ("✅ El bot volvió a publicar su estado. Todo normal.", 2)
        return None
    if LATIDO_EST["caido_t"] and ahora - LATIDO_EST["caido_t"] < 3 * H:
        return None
    LATIDO_EST["caido_t"] = ahora
    prio = 2 if es_noche(ahora) else (4 if hay_algo_abierto else 3)
    extra = (" Tienes una posición u orden abierta: el guardián la sigue protegiendo (corte, candado, salida y stop)."
             if hay_algo_abierto else " No hay posiciones ni órdenes abiertas: no hay dinero en riesgo.")
    return (f"⚠️ El bot {TXT_CAIDO} hace {edad_min:.0f} min (¿Render reiniciando o caído?).{extra}", prio)

# ───────── Ejecución ─────────
def avisar(cfg, texto, prioridad=4, http=requests):
    """v2.7: Telegram (sin sonido si es de noche o prioridad ≤ 2, salvo las 🚨)."""
    print(texto)
    if not (cfg.get("tg_token") and cfg.get("tg_chat")):
        return False
    silencio = ("🚨" not in texto) and (prioridad <= 2 or es_noche(int(time.time() * 1000)))
    try:
        r = http.post(f"https://api.telegram.org/bot{cfg['tg_token']}/sendMessage",
                      json={"chat_id": cfg["tg_chat"], "text": f"🛡️ Guardián TAO\n{texto}", "disable_notification": silencio},
                      timeout=15)
        if r.status_code != 200:
            print("telegram:", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        print("telegram:", e)
        return False

ULT_LATIDO = [0]

def correr(bx, cfg, ahora=None, precio_fn=None, capital_fn=None, http=requests, p12_fn=None):
    ahora = ahora or int(time.time() * 1000)
    hechos, abierto = [], False
    estado = ultimo_estado(cfg, http)
    for sym in cfg["simbolos"]:
        posiciones, tpsl, ordenes = bx.posiciones(sym), bx.tpsl(sym), bx.ordenes(sym)
        abierto = abierto or bool(posiciones) or any(str(o.get("clientId") or "").startswith(cfg["prefijo"]) for o in ordenes)
        if not posiciones:
            acciones = [(sym, None, a) for a in depurar_entradas_sin_posicion(ordenes, cfg)]
        else:
            precio = (precio_fn or bx.precio)(sym)
            cap = (capital_fn or bx.capital)()
            acciones = []
            for p in posiciones:
                manual = modo_pos(estado, p.get("positionId")) == "manual"
                p12 = precio_corte(bx, sym, p, ahora, cfg, p12_fn, manual)
                acciones += [(sym, p, a) for a in decidir(p, tpsl, ordenes, precio, ahora, cap, cfg, manual, p12,
                                                          salida_publicada(estado, p.get("positionId")))]
        for sym_, pos, a in acciones:
            txt = f"🛡️ {sym_}: " + (a[-1] if a[0] != "aviso" else a[1])
            if cfg["modo"] != "real" and a[0] != "aviso":
                txt += "  [MODO PAPEL: no ejecutado]"
            else:
                try:
                    if a[0] == "cerrar":
                        bx.cerrar(pos["positionId"])
                    elif a[0] == "stop":
                        if not mover_verificado(bx, sym_, pos, a[1]):
                            # v2.1: si no se puede CONFIRMAR el stop, se cierra para no regalar la ganancia/arriesgar de más
                            bx.cerrar(pos["positionId"])
                            txt = (f"🚨 {sym_}: no pude confirmar el stop en {a[1]:.2f} ({a[-1]}). "
                                   f"Cerré la posición a mercado para protegerla.")
                    elif a[0] == "cancelar":
                        bx.cancelar(sym_, a[1])
                except Exception as e:
                    txt = f"🚨 {sym_}: FALLÓ '{a[-1]}': {e}. Hazlo a mano."
            avisar(cfg, txt, 5 if "🚨" in txt else 4)
            hechos.append((a[0], txt))
    try:
        lat = revisar_latido(cfg, ahora, abierto, http)
        # v2.5: en modo ciclo no repite el mismo aviso de latido cada 10 min (sí si es urgente: hay posición abierta)
        if lat and (lat[1] >= 5 or ahora - ULT_LATIDO[0] >= 30 * 60000):
            ULT_LATIDO[0] = ahora
            avisar(cfg, lat[0], lat[1])
            hechos.append(("latido", lat[0]))
    except Exception as e:
        print("latido:", e)                     # un fallo leyendo GitHub nunca debe despertar a nadie
    return hechos

def config():
    return {"simbolos": env("SIMBOLOS", "TAOUSDT").split(","), "gracia_min": float(env("GRACIA_MIN", 10)),
            "riesgo_max": float(env("RIESGO_MAX", 2.0)), "modo": str(env("MODO", "papel")).strip().lower(), "prefijo": env("PREFIJO_BOT", "taol"),
            "tg_token": env("TELEGRAM_TOKEN"), "tg_chat": env("TELEGRAM_CHAT_ID"),
            "latido_min": max(30.0, float(env("LATIDO_MIN", 30))),
            "estado_token": env("ESTADO_TOKEN"), "estado_repo": env("ESTADO_REPO", "alanmontilva-hub/libro-tao"),
            "estado_rama": env("ESTADO_RAMA", "estado")}

if __name__ == "__main__":
    # v2.5: GitHub NO respeta el cron de 10 min en la práctica (corrió cada 3-6 h). Por eso cada ejecución
    # vigila en ciclo DURACION_MIN minutos (una revisión cada PAUSA_MIN) y el cron solo la relanza.
    cfg = config()
    bx = Bitunix(env("BITUNIX_API_KEY"), env("BITUNIX_SECRET_KEY"))
    dur, pausa = float(env("DURACION_MIN", 0)), max(1.0, float(env("PAUSA_MIN", 10)))
    fin = time.time() + dur * 60
    print(f"Guardián v2.7 · estado {'GitHub' if cfg['estado_token'] else 'SIN ESTADO_TOKEN'} · avisos "
          f"{'Telegram' if cfg['tg_token'] and cfg['tg_chat'] else 'SOLO EN EL LOG (faltan TELEGRAM_TOKEN/TELEGRAM_CHAT_ID)'} · MODO={cfg['modo'].upper()} "
          f"({'ejecuta de verdad' if cfg['modo'] == 'real' else 'PAPEL: solo avisa'}) · ciclo {dur:g} min, revisión cada {pausa:g} min")
    vuelta = 0
    while True:
        vuelta += 1
        try:
            r = correr(bx, cfg)
            print(f"[{time.strftime('%H:%M:%S')}] vuelta {vuelta}: {len(r)} acciones")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] vuelta {vuelta} falló: {e}")
        if time.time() + pausa * 60 > fin:
            break
        time.sleep(pausa * 60)
