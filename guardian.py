"""
Guardián TAO v2 (7-oct-2026) — corre en GitHub Actions, sin estado propio. No es asesoría financiera.

La única fuente de verdad es Bitunix:
  - hora de apertura  -> ctime de la posición
  - entrada           -> avgOpenPrice
  - stop vigente      -> orden TP/SL de la posición
  - candado 12h hecho -> el stop ya está del lado de la ganancia (o en entrada)

Reglas (en cada corrida, idempotentes):
  1. Sin stop en Bitunix           -> pone stop de emergencia (4%) y avisa 🚨
  2. Riesgo abierto > RIESGO_MAX % -> avisa 🚨 (detecta entradas dobles)
  3. Órdenes de entrada del bot duplicadas, o pendientes con posición abierta -> las cancela
  4. Edad >= 12h y candado no hecho: si pierde -> cierra; si gana -> stop asegura 90%
  5. Edad >= 24h                    -> cierra
  6. Latido del bot (v2, sin falsas alarmas):
     · avisa solo si el bot lleva más de LATIDO_MIN (por defecto 30) min sin publicar;
     · NUNCA como alarma: es una notificación normal (y silenciosa de 10 p.m. a 7 a.m. Caracas),
       porque el guardián ya protege cualquier posición abierta aunque el bot esté caído;
     · un solo aviso por caída (repite cada 3 h si sigue caído) y avisa cuando el bot vuelve.

Variables de entorno:
  BITUNIX_API_KEY, BITUNIX_SECRET_KEY   (llave SIN permiso de retiro)
  SIMBOLOS=TAOUSDT   GRACIA_MIN=10   RIESGO_MAX=2.0   MODO=real|papel
  NTFY_TOPIC=<canal>   NTFY_SERVER=https://ntfy.sh   LATIDO_TOPIC=<canal_estado>  LATIDO_MIN=30
  PREFIJO_BOT=taol
"""
import os, time, json, uuid, hashlib, requests
from datetime import datetime, timezone, timedelta

BASE = "https://fapi.bitunix.com"
H = 3600_000
CANDADO_H, HOLD_H, CANDADO = 12, 24, 0.90
STOP_EMERGENCIA = 0.04
CARACAS = timezone(timedelta(hours=-4))
NOCHE = (22, 7)                      # de 10 p.m. a 7 a.m. Caracas: avisos de latido en silencio
TXT_CAIDO, TXT_VOLVIO = "no publica estado", "volvió a publicar"

def env(k, d=None):
    v = os.environ.get(k)
    return v if v not in (None, "") else d

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
    def cancelar(self, sym, oids):
        return self._req("POST", "/api/v1/futures/trade/cancel_orders",
                         data={"symbol": sym, "orderList": [{"orderId": str(o)} for o in oids]})

# ───────── Lógica pura (sin red): decide qué hacer ─────────
def lado_de(pos):
    s = str(pos.get("side", "")).upper()
    return 1 if s in ("LONG", "BUY") else -1          # la doc dice LONG/SHORT; el historial usa BUY/SELL

def stop_de(pos, tpsl):
    for t in tpsl:
        if str(t.get("positionId")) == str(pos.get("positionId")) and t.get("slPrice") not in (None, "", "0"):
            return float(t["slPrice"]), True
    return None, False

def decidir(pos, tpsl, ordenes, precio, ahora, capital, cfg):
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
    if edad_h >= HOLD_H + gracia:
        acc.append(("cerrar", f"24h cumplidas ({edad_h:.1f}h)"))
        return acc
    if sl is None:
        sl = ent * (1 - STOP_EMERGENCIA * lado)
        acc.append(("stop", sl, False, "🚨 posición SIN stop en Bitunix: stop de emergencia al 4%"))
        existe = True
    riesgo = max(0.0, (ent - sl) * lado) * qty
    if capital and riesgo / capital * 100 > cfg["riesgo_max"]:
        acc.append(("aviso", f"🚨 riesgo abierto {riesgo:.2f} USDT = {riesgo / capital * 100:.1f}% del capital "
                             f"(máx {cfg['riesgo_max']}%). Posible entrada doble: qty {qty}."))
    candado_hecho = (sl - ent) * lado >= 0
    if edad_h >= CANDADO_H + gracia and not candado_hecho:
        g = (precio - ent) * lado
        if g <= 0:
            acc.append(("cerrar", f"revisión 12h: va perdiendo ({g:+.2f} por unidad)"))
        else:
            nuevo = ent + lado * CANDADO * g
            if (nuevo - sl) * lado > 0:
                acc.append(("stop", nuevo, existe, f"revisión 12h: gana {g:+.2f}; stop asegura 90% en {nuevo:.2f}"))
    return acc

def depurar_entradas_sin_posicion(ordenes, cfg):
    """Sin posición: si hay más de una orden de entrada del bot viva, deja solo la más nueva."""
    pref = cfg["prefijo"]
    e = sorted([o for o in ordenes if str(o.get("clientId") or "").startswith(pref) and not o.get("reduceOnly")],
               key=lambda o: int(o.get("ctime") or 0))
    if len(e) > 1:
        return [("cancelar", [o["orderId"] for o in e[:-1]], f"{len(e)} órdenes de entrada del bot vivas a la vez (entrada doble)")]
    return []

# ───────── Latido del bot (v2) ─────────
def es_noche(ahora_ms):
    h = datetime.fromtimestamp(ahora_ms / 1000, CARACAS).hour
    return h >= NOCHE[0] or h < NOCHE[1]

def leer_topic(cfg, topic, desde, http=requests):
    """Mensajes del canal ntfy desde hace `desde` (ej. '30m', '3h'). Devuelve lista de dicts."""
    r = http.get(f"{cfg['ntfy_server']}/{topic}/json", params={"poll": "1", "since": desde}, timeout=20)
    out = []
    for l in r.text.splitlines():
        try:
            j = json.loads(l)
            if j.get("event", "message") == "message":
                out.append(j)
        except ValueError:
            pass
    return out

def revisar_latido(cfg, ahora, hay_algo_abierto, http=requests):
    """Devuelve (texto, prioridad) o None. Un aviso por caída, sin alarma, y aviso de regreso."""
    if not cfg["latido_topic"] or cfg["latido_min"] <= 0:
        return None
    vivos = leer_topic(cfg, cfg["latido_topic"], f"{int(cfg['latido_min'])}m", http)
    propios = leer_topic(cfg, cfg["ntfy"], "3h", http) if cfg.get("ntfy") else []
    ult_caido = max((m.get("time", 0) for m in propios if TXT_CAIDO in (m.get("message") or "")), default=0)
    ult_volvio = max((m.get("time", 0) for m in propios if TXT_VOLVIO in (m.get("message") or "")), default=0)
    if vivos:
        if ult_caido and ult_caido > ult_volvio:
            return ("✅ El bot volvió a publicar su estado. Todo normal.", 2)
        return None
    if ult_caido and ult_caido > ult_volvio:
        return None                                # ya avisé de esta caída en las últimas 3 h
    ultimo = leer_topic(cfg, cfg["latido_topic"], "12h", http)
    hace = f"{(ahora / 1000 - max(m.get('time', 0) for m in ultimo)) / 60:.0f} min" if ultimo else "más de 12 h"
    prio = 2 if es_noche(ahora) else (4 if hay_algo_abierto else 3)
    extra = (" Tienes una posición u orden abierta: el guardián la sigue protegiendo (12h, 24h y stop)."
             if hay_algo_abierto else " No hay posiciones ni órdenes abiertas: no hay dinero en riesgo.")
    return (f"⚠️ El bot {TXT_CAIDO} hace {hace} (¿Render reiniciando o caído?).{extra}", prio)

# ───────── Ejecución ─────────
def avisar(cfg, texto, prioridad=4):
    print(texto)
    if cfg.get("ntfy"):
        try:
            requests.post(f"{cfg['ntfy_server']}/{cfg['ntfy']}", data=texto.encode(),
                          headers={"Title": "Guardián TAO", "Priority": str(prioridad)}, timeout=15)
        except Exception as e:
            print("ntfy:", e)

def correr(bx, cfg, ahora=None, precio_fn=None, capital_fn=None, http=requests):
    ahora = ahora or int(time.time() * 1000)
    hechos, abierto = [], False
    for sym in cfg["simbolos"]:
        posiciones, tpsl, ordenes = bx.posiciones(sym), bx.tpsl(sym), bx.ordenes(sym)
        abierto = abierto or bool(posiciones) or any(str(o.get("clientId") or "").startswith(cfg["prefijo"]) for o in ordenes)
        if not posiciones:
            acciones = [(sym, None, a) for a in depurar_entradas_sin_posicion(ordenes, cfg)]
        else:
            precio = (precio_fn or bx.precio)(sym)
            cap = (capital_fn or bx.capital)()
            acciones = [(sym, p, a) for p in posiciones for a in decidir(p, tpsl, ordenes, precio, ahora, cap, cfg)]
        for sym_, pos, a in acciones:
            txt = f"🛡️ {sym_}: " + (a[-1] if a[0] != "aviso" else a[1])
            if cfg["modo"] != "real" and a[0] != "aviso":
                txt += "  [MODO PAPEL: no ejecutado]"
            else:
                try:
                    if a[0] == "cerrar":
                        bx.cerrar(pos["positionId"])
                    elif a[0] == "stop":
                        bx.poner_stop(sym_, pos["positionId"], a[1], a[2])
                    elif a[0] == "cancelar":
                        bx.cancelar(sym_, a[1])
                except Exception as e:
                    txt = f"🚨 {sym_}: FALLÓ '{a[-1]}': {e}. Hazlo a mano."
            avisar(cfg, txt, 5 if "🚨" in txt else 4)
            hechos.append((a[0], txt))
    try:
        lat = revisar_latido(cfg, ahora, abierto, http)
        if lat:
            avisar(cfg, lat[0], lat[1])
            hechos.append(("latido", lat[0]))
    except Exception as e:
        print("latido:", e)                     # un fallo leyendo ntfy nunca debe despertar a nadie
    return hechos

def config():
    return {"simbolos": env("SIMBOLOS", "TAOUSDT").split(","), "gracia_min": float(env("GRACIA_MIN", 10)),
            "riesgo_max": float(env("RIESGO_MAX", 2.0)), "modo": env("MODO", "papel"), "prefijo": env("PREFIJO_BOT", "taol"),
            "ntfy": env("NTFY_TOPIC"), "ntfy_server": env("NTFY_SERVER", "https://ntfy.sh"),
            "latido_topic": env("LATIDO_TOPIC"), "latido_min": max(30.0, float(env("LATIDO_MIN", 30)))}

if __name__ == "__main__":
    cfg = config()
    bx = Bitunix(env("BITUNIX_API_KEY"), env("BITUNIX_SECRET_KEY"))
    r = correr(bx, cfg)
    print(f"Guardián v2: {len(r)} acciones · modo {cfg['modo']}")
