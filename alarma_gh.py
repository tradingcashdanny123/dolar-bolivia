"""ALARMA DEL DOLAR Y DEL EURO en bolivianos -> aviso por ntfy. Corre en GitHub Actions (no necesita ninguna compu prendida).
Solo LEE precios publicos y avisa. No entra a ninguna cuenta, no compra ni vende.
Cada ejecucion hace UNA revision y guarda su memoria en estado.json (GitHub la conserva entre ejecuciones).

Uso:
  python3 alarma_gh.py          -> una revision normal
  python3 alarma_gh.py --ver    -> muestra los precios y de donde salen, sin avisar
  python3 alarma_gh.py --test   -> manda un aviso de prueba con los precios
El canal de ntfy viene de la variable de entorno NTFY_TOPIC (no se escribe en el codigo).
"""
import json, os, sys, time, copy, urllib.request, datetime as dt
from statistics import median

TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PASO = {"dolar": 0.05, "euro": 0.05}      # avisa cada vez que se mueve esto (Bs)
MONTO_BS = 1000                           # precio para cambiar mas o menos este monto
HORA_RESUMEN = 8                          # hora de Bolivia (UTC-4)
ESTADO = os.environ.get("ESTADO", "estado.json")
NOMBRE = {"dolar": "dólar", "euro": "euro"}
UA = "Mozilla/5.0"


def pedir(url, cuerpo=None, cab=None, timeout=20):
    h = {"User-Agent": UA}
    h.update(cab or {})
    data = None
    if cuerpo is not None:
        data = json.dumps(cuerpo).encode("utf-8")
        h["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
        return r.read().decode("utf-8")


def avisar(titulo, texto, prioridad=3):
    if not TOPIC:
        raise RuntimeError("falta NTFY_TOPIC")
    ultimo = None
    for i in range(3):
        try:
            pedir("https://ntfy.sh/", {"topic": TOPIC, "title": titulo, "message": texto, "priority": prioridad})
            return
        except Exception as e:                       # nunca imprimir la direccion: solo el tipo de error
            ultimo = f"{type(e).__name__} {getattr(e, 'code', '')}"
            time.sleep(3 * (i + 1))
    raise RuntimeError("ntfy no acepto el aviso: " + str(ultimo))


def lado_binance(tipo):
    """BUY = lo que pagas por un dolar; SELL = lo que te dan. Mitad de los 5 mejores anuncios con historial."""
    j = json.loads(pedir("https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
                         {"fiat": "BOB", "asset": "USDT", "tradeType": tipo, "page": 1, "rows": 20,
                          "payTypes": [], "publisherType": None, "transAmount": str(MONTO_BS)}))
    p = sorted(float(d["adv"]["price"]) for d in (j.get("data") or [])
               if (d["advertiser"].get("monthOrderCount") or 0) >= 50 and (d["advertiser"].get("monthFinishRate") or 0) >= 0.95)[:5]
    if len(p) < 3:
        raise ValueError("pocos anuncios confiables")
    return p[len(p) // 2]


def euro_en_dolares():
    """Dukascopy (mercado en vivo) y, si falla, open.er-api (1 vez al dia)."""
    try:
        t = pedir("https://freeserv.dukascopy.com/2.0/?path=chart/json3&instrument=EUR%2FUSD&offer_side=B&interval=1MIN"
                  f"&splits=true&stocks=true&limit=3&time_direction=P&timestamp={int(time.time() * 1000)}&jsonp=_cb",
                  cab={"Referer": "https://freeserv.dukascopy.com/2.0/?path=chart/index"})
        f = sorted(json.loads(t[t.index("(") + 1: t.rindex(")")]), key=lambda r: -r[0])
        x = float(f[0][4])
        if 0.8 < x < 1.6:
            return x, "Dukascopy"
    except Exception as e:
        print("euro dukascopy:", type(e).__name__)
    x = float(json.loads(pedir("https://open.er-api.com/v6/latest/EUR"))["rates"]["USD"])
    if 0.8 < x < 1.6:
        return x, "open.er-api"
    raise ValueError("euro sin precio")


def leer():
    fuente = "Binance"
    try:
        dolar = {"compra": lado_binance("BUY"), "venta": lado_binance("SELL")}
    except Exception as e:
        print("binance directo fallo:", type(e).__name__, getattr(e, "code", ""))
        c = json.loads(pedir(f"https://criptoya.com/api/binancep2p/USDT/BOB/{MONTO_BS}"))
        dolar = {"compra": float(c["ask"]), "venta": float(c["bid"])}
        fuente = "CriptoYa (copia de Binance)"
    if not (dolar["compra"] > 0 and dolar["venta"] > 0):
        raise ValueError("precio del dolar vacio")
    out = {"fuente": fuente, "dolar": dolar}
    try:
        x, fx = euro_en_dolares()
        out["euro"] = {"compra": dolar["compra"] * x, "venta": dolar["venta"] * x}
        out["fuente_euro"] = fx
    except Exception as e:
        print("euro:", type(e).__name__)
    return out


def bs(n):
    return f"{n:.2f}".replace(".", ",")


def texto(p):
    return f"Comprar: pagas {bs(p['compra'])} Bs\nVender: te dan {bs(p['venta'])} Bs"


def record(h, p, signo, hora):
    for dur, nombre in ((168, "los últimos 7 días"), (24, "las últimas 24 horas")):
        desde = hora - dur
        if not h or h[0][0] > desde + 2:
            continue                                  # todavia no hay datos de todo ese tiempo
        t = [f for f in h if f[0] >= desde]
        if signo < 0 and p["compra"] <= min(f[1] for f in t):
            return f"\n⭐ El MÁS BARATO de {nombre} para comprar"
        if signo > 0 and p["venta"] >= max(f[2] for f in t):
            return f"\n⭐ El MÁS CARO de {nombre} para vender"
    return ""


def procesar(moneda, s, p, ahora):
    """Actualiza el estado s y devuelve (titulo, texto, prioridad) o None."""
    hora = int(ahora // 3600)
    medio = (p["compra"] + p["venta"]) / 2
    if s.get("ref") is None:
        s["ref"] = medio
    dif = medio - s["ref"]
    signo = 0 if abs(dif) < PASO[moneda] - 1e-9 else (1 if dif > 0 else -1)
    aviso = None
    if signo == 0:
        s["pend"] = 0
    elif s.get("pend") != signo:
        s["pend"] = signo                             # primera vez: espera la siguiente lectura para confirmar
    else:
        extra = record(s["h"], p, signo, hora)
        verbo = "🔺 El %s SUBIÓ " if signo > 0 else "🔻 El %s BAJÓ "
        aviso = ((verbo % NOMBRE[moneda]) + bs(abs(dif)) + " Bs", texto(p) + extra, 5 if extra else 4)
        s["ref"], s["pend"] = medio, 0
    if s.get("pend", 0) == 0:
        c, v = round(p["compra"], 3), round(p["venta"], 3)
        if s["h"] and s["h"][-1][0] == hora:
            s["h"][-1][1] = min(s["h"][-1][1], c)
            s["h"][-1][2] = max(s["h"][-1][2], v)
        else:
            s["h"].append([hora, c, v])
        s["h"] = [f for f in s["h"] if f[0] >= hora - 169]
    return aviso


def resumen(precios):
    return "💵 DÓLAR\n" + texto(precios["dolar"]) + ("\n💶 EURO\n" + texto(precios["euro"]) if "euro" in precios else "")


def cargar():
    try:
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def guardar(e):
    tmp = ESTADO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(e, f)
    os.replace(tmp, ESTADO)


def revisar(ahora=None, leer_fn=None, avisar_fn=None):
    ahora = ahora if ahora is not None else time.time()
    leer_fn = leer_fn or leer
    avisar_fn = avisar_fn or avisar
    e = cargar()
    primera = "ok" not in e and "dolar" not in e
    hoy = dt.datetime.fromtimestamp(ahora - 4 * 3600, dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        precios = leer_fn()
    except Exception as ex:
        print("sin precio:", type(ex).__name__, getattr(ex, "code", ""))
        e.setdefault("ok", ahora)
        if ahora - e["ok"] > 1800 and not e.get("caida"):
            try:
                avisar_fn("⚠️ Dólar: sin datos", "Hace 30 minutos que no puedo leer el precio. Sigo intentando.", 3)
                e["caida"] = True
            except Exception as ex2:
                print("aviso fallo:", ex2)
        guardar(e)
        return
    if e.get("caida"):
        try:
            avisar_fn("✅ Dólar: ya volvió", "Otra vez puedo leer el precio.", 3)
            e["caida"] = False
        except Exception as ex2:
            print("aviso fallo:", ex2)
    e["ok"] = ahora
    if primera:
        e["dia"] = hoy                               # recien instalada: el primer resumen es mañana
    for m in ("dolar", "euro"):
        if m not in precios:
            continue
        previo = e.get(m) or {"ref": None, "pend": 0, "h": []}
        s = copy.deepcopy(previo)
        a = procesar(m, s, precios[m], ahora)
        if a:
            try:
                avisar_fn(*a)
                print("aviso enviado:", a[0])
            except Exception as ex2:                 # no se pierde: el estado no cambia y se reintenta en la proxima
                print("aviso fallo, se reintenta:", ex2)
                continue
        e[m] = s
    local = dt.datetime.fromtimestamp(ahora - 4 * 3600, dt.timezone.utc)
    if local.hour >= HORA_RESUMEN and e.get("dia") != hoy:
        try:
            avisar_fn("☀️ Dólar y euro de hoy", resumen(precios) + "\n(Este aviso también te dice que la alarma está viva)", 3)
            e["dia"] = hoy
            print("resumen enviado")
        except Exception as ex2:
            print("resumen fallo:", ex2)
    guardar(e)
    print("revision lista | dolar", bs(precios["dolar"]["compra"]), "/", bs(precios["dolar"]["venta"]), "| fuente", precios["fuente"])


if __name__ == "__main__":
    if "--ver" in sys.argv:
        p = leer()
        print("Fuente dolar:", p["fuente"], "| euro:", p.get("fuente_euro", "sin euro"))
        print(resumen(p))
    elif "--test" in sys.argv:
        p = leer()
        avisar("✅ Alarma de dólar y euro: prueba", resumen(p) + "\nFuente: " + p["fuente"], 4)
        print("aviso de prueba enviado")
    else:
        revisar()
