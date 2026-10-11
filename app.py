import os, re, csv, io, secrets
from datetime import date, timedelta
from decimal import Decimal as D, ROUND_HALF_UP, InvalidOperation
from functools import wraps
import psycopg
from psycopg import errors as pgerr
from psycopg.rows import dict_row
from flask import (Flask, g, request, session, redirect, url_for,
                   render_template_string, flash, abort, Response)
from jinja2 import DictLoader
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/sistema_ve")
PROD = os.environ.get("APP_ENV") == "production"
DEMO = os.environ.get("DEMO_MODE") == "1"   # datos ficticios + banner; ver LEEME
KEYF = os.path.join(BASE, ".secret_key")
if not os.environ.get("SECRET_KEY") and not os.path.exists(KEYF):
    open(KEYF, "w").write(secrets.token_hex(32))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or open(KEYF).read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=PROD)
if PROD:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Alícuotas: G general, R reducida, E exento, L lujo (general 16% + adicional 15%). Ajustar si cambia la ley.
ALIC = {"G": D(16), "R": D(8), "E": D(0), "L": D(31)}
COL = {"E": "exenta", "G": "general", "R": "reducida", "L": "lujo"}
IGTF = D(3)  # % sobre pagos en divisas
DOCT = {"FACT": "Factura", "ND": "Nota de débito", "NC": "Nota de crédito"}
ROLES = {"inventario": "Inventario", "usuarios": "Usuarios", "admin": "Administrador"}
RIF_RE = re.compile(r"^[JGVEPC]-?\d{7,9}-?\d?$", re.I)

def money(x): return D(x).quantize(D("0.01"), ROUND_HALF_UP)

def num(v, default=None):
    try:
        x = D(str(v).strip().replace(",", "."))
        return x if x.is_finite() else default
    except (InvalidOperation, AttributeError):
        return default

def dfecha(s):
    try: return date.fromisoformat(s)
    except (ValueError, TypeError): return date.today()

# ---------- Seguridad HTTPS / cabeceras ----------
@app.before_request
def https_only():
    if PROD and not request.is_secure:
        return redirect(request.url.replace("http://", "https://", 1), 301)

@app.after_request
def headers(r):
    r.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
                      "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'"})
    if PROD: r.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return r

# ---------- DB (PostgreSQL) ----------
def db():
    if "db" not in g: g.db = psycopg.connect(DSN, row_factory=dict_row)
    return g.db

def q(sql, args=(), one=False):
    r = db().execute(sql, args).fetchall()
    return (r[0] if r else None) if one else r

def ex(sql, args=()): return db().execute(sql, args)

@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d: d.close()

SCHEMA = """
create table if not exists users(id serial primary key, usuario text unique not null, nombre text, clave text not null,
  rol text not null, activo boolean not null default true);
create table if not exists productos(id serial primary key, codigo text unique not null, descripcion text not null,
  unidad text default 'UND', stock numeric(18,4) default 0, costo numeric(18,4) default 0, precio numeric(18,4) default 0,
  alicuota char(1) default 'G', minimo numeric(18,4) default 0, activo boolean default true);
create table if not exists movimientos(id serial primary key, fecha date, producto_id int references productos(id),
  tipo text, cantidad numeric(18,4), ref text, usuario text);
create table if not exists islr_conceptos(id serial primary key, codigo text unique not null, descripcion text,
  porcentaje numeric(6,2) not null, activo boolean default true);
create table if not exists docs(id serial primary key, tipo text not null, doc_tipo text not null default 'FACT',
  nro_afectada text, fecha date not null, rif text not null, nombre text, nro_factura text not null, nro_control text,
  base_exenta numeric(18,2) default 0, base_general numeric(18,2) default 0, base_reducida numeric(18,2) default 0,
  base_lujo numeric(18,2) default 0, iva_general numeric(18,2) default 0, iva_reducida numeric(18,2) default 0,
  iva_lujo numeric(18,2) default 0, total numeric(18,2) default 0, retencion numeric(18,2) default 0,
  islr_pct numeric(6,2) default 0, islr_monto numeric(18,2) default 0, igtf numeric(18,2) default 0,
  condicion text, vence date, saldo numeric(18,2) default 0, usuario text, unique(tipo, rif, doc_tipo, nro_factura));
create table if not exists doc_lineas(id serial primary key, doc_id int references docs(id), producto_id int references productos(id),
  cantidad numeric(18,4), precio numeric(18,4), alicuota char(1));
create table if not exists pagos(id serial primary key, doc_id int references docs(id), fecha date, monto numeric(18,2),
  igtf numeric(18,2) default 0, ref text, usuario text);
"""

def init_db():
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        c.execute("select pg_advisory_xact_lock(7001)")
        c.execute(SCHEMA)
        if not c.execute("select 1 from users").fetchone():
            for u, n, r in [("admin", "Administrador", "admin"), ("inventario", "Encargado de inventario", "inventario"),
                            ("usuarios", "Gestor de usuarios", "usuarios")]:
                pw = os.environ.get(f"PASS_{u.upper()}") or secrets.token_urlsafe(9)
                c.execute("insert into users(usuario,nombre,clave,rol) values(%s,%s,%s,%s)", (u, n, generate_password_hash(pw), r))
                print(f"USUARIO INICIAL  {u} / {pw}   (cámbiela al ingresar)")
        if DEMO and not c.execute("select 1 from productos").fetchone():
            seed_demo(c)

def _seed_doc(c, tipo, dias_atras, rif, nombre, nf, lines, cond="contado", plazo=30):
    fecha = date.today() - timedelta(days=dias_atras)
    bases, rows = {k: D(0) for k in ALIC}, []
    for cod, cant, pr in lines:
        p = c.execute("select * from productos where codigo=%s", (cod,)).fetchone()
        cant, pr = D(cant), D(str(pr))
        bases[p["alicuota"]] += money(cant * pr); rows.append((p, cant, pr))
    ivas = {k: money(bases[k] * ALIC[k] / 100) for k in ALIC}
    total = money(sum(bases.values()) + sum(ivas.values()))
    vence = fecha + timedelta(days=plazo) if cond == "credito" else None
    did = c.execute("insert into docs(tipo,doc_tipo,fecha,rif,nombre,nro_factura,nro_control,base_exenta,base_general,base_reducida,"
                    "base_lujo,iva_general,iva_reducida,iva_lujo,total,condicion,vence,saldo,usuario) values(%s,'FACT',%s,%s,%s,%s,%s,"
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'demo') returning id",
                    (tipo, fecha, rif, nombre, nf, "00-" + nf, bases["E"], bases["G"], bases["R"], bases["L"],
                     ivas["G"], ivas["R"], ivas["L"], total, cond, vence, total if cond == "credito" else D(0))).fetchone()["id"]
    for p, cant, pr in rows:
        d = cant if tipo == "compra" else -cant
        c.execute("insert into doc_lineas(doc_id,producto_id,cantidad,precio,alicuota) values(%s,%s,%s,%s,%s)", (did, p["id"], cant, pr, p["alicuota"]))
        c.execute("update productos set stock=stock+%s where id=%s", (d, p["id"]))
        c.execute("insert into movimientos(fecha,producto_id,tipo,cantidad,ref,usuario) values(%s,%s,%s,%s,%s,'demo')", (fecha, p["id"], f"{tipo}_FACT", d, nf))

def seed_demo(c):
    for cod, desc, al, costo, precio in [("D-001", "Camisa de algodón", "G", 8, 14), ("D-002", "Pantalón jean", "G", 15, 26),
            ("D-003", "Arroz 1 kg", "E", "1.2", "1.8"), ("D-004", "Aceite 1 L", "R", "2.5", "3.6"),
            ("D-005", "Perfume importado", "L", 20, 38), ("D-006", "Harina de maíz 1 kg", "E", 1, "1.5")]:
        c.execute("insert into productos(codigo,descripcion,alicuota,costo,precio,minimo) values(%s,%s,%s,%s,%s,5)", (cod, desc, al, D(str(costo)), D(str(precio))))
    c.execute("insert into islr_conceptos(codigo,descripcion,porcentaje) values('DEMO-SERV','Concepto de ejemplo (demo)',2)")
    _seed_doc(c, "compra", 40, "J-30123456-7", "Textiles del Centro, C.A.", "000101", [("D-001", 60, 8), ("D-002", 40, 15)])
    _seed_doc(c, "compra", 35, "J-29876543-1", "Alimentos Andinos, C.A.", "000245", [("D-003", 200, "1.2"), ("D-006", 150, 1), ("D-004", 80, "2.5")], "credito", 30)
    _seed_doc(c, "compra", 20, "J-31555222-0", "Importadora Lujo, C.A.", "000310", [("D-005", 20, 20)], "credito", 45)
    _seed_doc(c, "venta", 30, "V-12345678-9", "María Pérez", "0001", [("D-001", 5, 14), ("D-002", 3, 26)])
    _seed_doc(c, "venta", 25, "J-40111222-3", "Bodega El Sol, C.A.", "0002", [("D-003", 40, "1.8"), ("D-006", 30, "1.5"), ("D-004", 12, "3.6")], "credito", 15)
    _seed_doc(c, "venta", 10, "J-40999888-1", "Boutique Aurora, C.A.", "0003", [("D-001", 20, 14), ("D-002", 10, 26), ("D-005", 4, 38)], "credito", 30)
    _seed_doc(c, "venta", 3, "V-20111222-5", "Carlos Rodríguez", "0004", [("D-005", 1, 38), ("D-001", 2, 14)])

def reset_demo():
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        c.execute("truncate pagos, doc_lineas, docs, movimientos, productos, islr_conceptos restart identity cascade")
        seed_demo(c)

# ---------- Auth ----------
def login_required(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if "uid" not in session: return redirect(url_for("login"))
            if roles and session["rol"] not in roles: abort(403)
            return f(*a, **k)
        return w
    return deco

@app.before_request
def csrf():
    if request.method == "POST" and not secrets.compare_digest(request.form.get("_t", ""), session.get("_t", "x")):
        abort(400)

@app.context_processor
def ctx():
    session.setdefault("_t", secrets.token_hex(16))
    creds = [(u, os.environ.get(f"PASS_{u.upper()}")) for u in ("admin", "inventario", "usuarios")] \
        if DEMO and os.environ.get("DEMO_SHOW_CREDS") == "1" else []
    return {"T": f'<input type=hidden name=_t value="{session["_t"]}">', "ROLES": ROLES, "DOCT": DOCT,
            "DEMO": DEMO, "DEMO_CREDS": [(u, p) for u, p in creds if p]}

@app.template_filter("bs")
def bs(x): return f"{(x or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

@app.template_filter("qty")
def qty(x): return "" if x is None else format(D(x).normalize(), "f")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("select * from users where usuario=%s and activo", (request.form["usuario"].strip(),), one=True)
        if u and check_password_hash(u["clave"], request.form["clave"]):
            session.clear(); session.update(uid=u["id"], usuario=u["usuario"], rol=u["rol"], _t=secrets.token_hex(16))
            return redirect(url_for("home"))
        flash("Usuario o clave incorrectos")
    return render_template_string(T_LOGIN)

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.route("/")
@login_required()
def home():
    return redirect({"inventario": "/productos", "usuarios": "/usuarios", "admin": "/doc/venta"}[session["rol"]])

@app.route("/clave", methods=["GET", "POST"])
@login_required()
def clave():
    if request.method == "POST" and DEMO:
        flash("No disponible en la versión demo"); return redirect(url_for("home"))
    if request.method == "POST":
        u = q("select * from users where id=%s", (session["uid"],), one=True)
        n = request.form["nueva"]
        if not check_password_hash(u["clave"], request.form["actual"]) or len(n) < 8:
            flash("Clave actual incorrecta o nueva menor a 8 caracteres")
        else:
            ex("update users set clave=%s where id=%s", (generate_password_hash(n), u["id"])); db().commit()
            flash("Clave actualizada"); return redirect(url_for("home"))
    return render_template_string(T_CLAVE)

# ---------- Usuarios ----------
@app.route("/usuarios", methods=["GET", "POST"])
@login_required("usuarios")
def usuarios():
    if request.method == "POST" and DEMO and request.form["accion"] != "crear":
        flash("No disponible en la versión demo"); return redirect(url_for("usuarios"))
    if request.method == "POST":
        f = request.form
        if f["accion"] == "crear":
            if f["rol"] not in ROLES or len(f["clave"]) < 8 or not f["usuario"].strip():
                flash("Datos inválidos (clave mínimo 8 caracteres)")
            else:
                try:
                    ex("insert into users(usuario,nombre,clave,rol) values(%s,%s,%s,%s)",
                       (f["usuario"].strip(), f["nombre"].strip(), generate_password_hash(f["clave"]), f["rol"])); db().commit()
                except pgerr.UniqueViolation:
                    db().rollback(); flash("Ese usuario ya existe")
        elif f["accion"] == "estado" and int(f["id"]) != session["uid"]:
            ex("update users set activo = not activo where id=%s", (f["id"],)); db().commit()
        elif f["accion"] == "reset" and len(f["clave"]) >= 8:
            ex("update users set clave=%s where id=%s", (generate_password_hash(f["clave"]), f["id"])); db().commit()
        return redirect(url_for("usuarios"))
    return render_template_string(T_USUARIOS, users=q("select * from users order by usuario"))

# ---------- Inventario ----------
@app.route("/productos", methods=["GET", "POST"])
@login_required("inventario", "admin")
def productos():
    if request.method == "POST":
        if session["rol"] != "inventario": abort(403)
        f = request.form
        if f["accion"] == "crear":
            try:
                ex("insert into productos(codigo,descripcion,unidad,costo,precio,alicuota,minimo) values(%s,%s,%s,%s,%s,%s,%s)",
                   (f["codigo"].strip(), f["descripcion"].strip(), f["unidad"].strip() or "UND", num(f["costo"], D(0)),
                    num(f["precio"], D(0)), f["alicuota"] if f["alicuota"] in ALIC else "G", num(f["minimo"], D(0)))); db().commit()
            except pgerr.UniqueViolation:
                db().rollback(); flash("Código duplicado")
        elif f["accion"] == "ajuste":
            c, tipo = num(f["cantidad"]), f["tipo"]
            p = q("select * from productos where id=%s", (f["id"],), one=True)
            if not p or not c or c <= 0 or tipo not in ("entrada", "salida") or not f["motivo"].strip():
                flash("Ajuste inválido (cantidad y motivo requeridos)")
            elif tipo == "salida" and p["stock"] < c: flash("Stock insuficiente")
            else:
                d = c if tipo == "entrada" else -c
                ex("update productos set stock=stock+%s where id=%s", (d, p["id"]))
                ex("insert into movimientos(fecha,producto_id,tipo,cantidad,ref,usuario) values(%s,%s,%s,%s,%s,%s)",
                   (date.today(), p["id"], "ajuste_" + tipo, d, f["motivo"].strip(), session["usuario"])); db().commit()
        return redirect(url_for("productos"))
    return render_template_string(T_PROD, prods=q("select * from productos where activo order by descripcion"),
        movs=q("select m.*,p.descripcion from movimientos m join productos p on p.id=m.producto_id order by m.id desc limit 30"))

# ---------- Compras / Ventas (facturas, ND, NC) ----------
def delta(tipo, dt, c):
    if dt == "ND": return D(0)
    if dt == "NC": return c if tipo == "venta" else -c   # NC venta: devolución entra; NC compra: sale
    return c if tipo == "compra" else -c

@app.route("/doc/<tipo>")
@login_required("admin")
def doc_lista(tipo):
    if tipo not in ("compra", "venta"): abort(404)
    return render_template_string(T_DOCS, tipo=tipo,
        docs=q("select * from docs where tipo=%s order by fecha desc,id desc limit 200", (tipo,)))

@app.route("/doc/<tipo>/nuevo", methods=["GET", "POST"])
@login_required("admin")
def doc_nuevo(tipo):
    if tipo not in ("compra", "venta"): abort(404)
    if request.method == "GET":
        return render_template_string(T_FORM, tipo=tipo, hoy=date.today(),
            prods=q("select * from productos where activo order by descripcion"),
            conceptos=q("select * from islr_conceptos where activo order by codigo"))
    f = request.form
    dt = f.get("doc_tipo", "FACT")
    if dt not in DOCT: abort(400)
    fecha, rif, nf, naf = dfecha(f.get("fecha")), f["rif"].strip().upper(), f["nro_factura"].strip(), f.get("nro_afectada", "").strip()
    lines, net = [], {}
    for i in range(8):
        if not f.get(f"p{i}"): continue
        p = q("select * from productos where id=%s", (f[f"p{i}"],), one=True)
        c, pr = num(f.get(f"c{i}")), num(f.get(f"pr{i}"))
        if p and c and c > 0 and pr is not None and pr >= 0:
            lines.append((p, c, pr)); net[p["id"]] = net.get(p["id"], D(0)) + delta(tipo, dt, c)
    err, aff = None, None
    if not lines: err = "Agregue al menos una línea válida"
    elif not RIF_RE.match(rif): err = "RIF inválido (ej. J-12345678-9)"
    elif not nf or not f["nro_control"].strip(): err = "Nº de documento y de control son obligatorios"
    elif q("select 1 from docs where tipo=%s and rif=%s and doc_tipo=%s and nro_factura=%s", (tipo, rif, dt, nf), one=True):
        err = "Documento ya registrado para ese RIF"
    elif dt != "FACT":
        aff = q("select * from docs where tipo=%s and rif=%s and doc_tipo='FACT' and nro_factura=%s", (tipo, rif, naf), one=True)
        if not aff: err = "La factura afectada no existe para ese RIF"
    if not err:
        for pid, d in net.items():
            p = q("select * from productos where id=%s", (pid,), one=True)
            if p["stock"] + d < 0: err = f"Stock insuficiente: {p['descripcion']} (disp. {qty(p['stock'])})"
    if err:
        flash(err); return redirect(request.url)

    bases = {k: D(0) for k in ALIC}
    for p, c, pr in lines: bases[p["alicuota"]] += money(c * pr)
    ivas = {k: money(bases[k] * ALIC[k] / 100) for k in ALIC}
    sin_iva, iva_t = sum(bases.values()), sum(ivas.values())
    total = money(sin_iva + iva_t)
    rp = num(f.get("ret_pct"), D(0)); rp = rp if rp in (0, 75, 100) else D(0)
    ret = money(iva_t * rp / 100)
    conc = q("select * from islr_conceptos where id=%s and activo", (f["islr"],), one=True) if f.get("islr") else None
    ipct = conc["porcentaje"] if conc else D(0)
    islr = money(sin_iva * ipct / 100)
    cond = "credito" if f.get("condicion") == "credito" and dt != "NC" else "contado"
    vence = fecha + timedelta(days=int(num(f.get("dias"), D(30)))) if cond == "credito" else None
    neto = money(total - ret - islr)
    saldo = neto if cond == "credito" else D(0)
    igtf = money(total * IGTF / 100) if cond == "contado" and f.get("divisas") and dt != "NC" else D(0)
    s = D(-1) if dt == "NC" else D(1)
    vals = [tipo, dt, naf or None, fecha, rif, f["nombre"].strip(), nf, f["nro_control"].strip(),
            bases["E"] * s, bases["G"] * s, bases["R"] * s, bases["L"] * s, ivas["G"] * s, ivas["R"] * s, ivas["L"] * s,
            total * s, ret * s, ipct, islr * s, igtf, cond, vence, saldo, session["usuario"]]
    did = ex("insert into docs(tipo,doc_tipo,nro_afectada,fecha,rif,nombre,nro_factura,nro_control,base_exenta,base_general,"
             "base_reducida,base_lujo,iva_general,iva_reducida,iva_lujo,total,retencion,islr_pct,islr_monto,igtf,condicion,"
             "vence,saldo,usuario) values(" + ",".join(["%s"] * 24) + ") returning id", vals).fetchone()["id"]
    for p, c, pr in lines:
        ex("insert into doc_lineas(doc_id,producto_id,cantidad,precio,alicuota) values(%s,%s,%s,%s,%s)", (did, p["id"], c, pr, p["alicuota"]))
        d = delta(tipo, dt, c)
        if d:
            ex("update productos set stock=stock+%s where id=%s", (d, p["id"]))
            ex("insert into movimientos(fecha,producto_id,tipo,cantidad,ref,usuario) values(%s,%s,%s,%s,%s,%s)",
               (fecha, p["id"], f"{tipo}_{dt}", d, nf, session["usuario"]))
        if tipo == "compra" and dt == "FACT": ex("update productos set costo=%s where id=%s", (pr, p["id"]))
    if dt == "NC" and aff["condicion"] == "credito":
        ex("update docs set saldo=greatest(saldo-%s,0) where id=%s", (neto, aff["id"]))
    db().commit()
    return redirect(url_for("doc_lista", tipo=tipo))

# ---------- Libros ----------
def rango(mes):
    try: y, m = map(int, mes.split("-")); d1 = date(y, m, 1)
    except (ValueError, AttributeError): d1 = date.today().replace(day=1)
    return d1, (d1.replace(day=28) + timedelta(days=4)).replace(day=1)

def libro_rows(tipo, mes):
    d1, d2 = rango(mes)
    return q("select * from docs where tipo=%s and fecha>=%s and fecha<%s order by fecha,id", (tipo, d1, d2))

KEYS = ["base_exenta", "base_general", "base_reducida", "base_lujo", "iva_general", "iva_reducida", "iva_lujo", "total", "retencion"]

@app.route("/libro/<tipo>")
@login_required("admin")
def libro(tipo):
    if tipo not in ("compra", "venta"): abort(404)
    mes = request.args.get("mes") or date.today().strftime("%Y-%m")
    rows = libro_rows(tipo, mes)
    return render_template_string(T_LIBRO, tipo=tipo, mes=mes, rows=rows, tot={k: sum(r[k] for r in rows) for k in KEYS})

@app.route("/libro/<tipo>.csv")
@login_required("admin")
def libro_csv(tipo):
    if tipo not in ("compra", "venta"): abort(404)
    mes = request.args.get("mes") or date.today().strftime("%Y-%m")
    out = io.StringIO(); w = csv.writer(out, delimiter=";")
    w.writerow(["Fecha", "Tipo", "RIF", "Razón social", "Nº documento", "Nº control", "Doc. afectado"] + KEYS)
    for r in libro_rows(tipo, mes):
        w.writerow([r["fecha"], r["doc_tipo"], r["rif"], r["nombre"], r["nro_factura"], r["nro_control"], r["nro_afectada"] or ""] + [r[k] for k in KEYS])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=libro_{tipo}s_{mes}.csv"})

# ---------- CxC / CxP ----------
@app.route("/cuentas/<tipo>")
@login_required("admin")
def cuentas(tipo):
    if tipo not in ("cobrar", "pagar"): abort(404)
    rows = q("select * from docs where tipo=%s and condicion='credito' and saldo>0 order by vence", ("venta" if tipo == "cobrar" else "compra",))
    return render_template_string(T_CTAS, tipo=tipo, rows=rows, hoy=date.today(), total=sum(r["saldo"] for r in rows))

@app.route("/pagar/<int:did>", methods=["POST"])
@login_required("admin")
def pagar(did):
    d = q("select * from docs where id=%s", (did,), one=True) or abort(404)
    m = num(request.form["monto"]); m = money(m) if m else None
    if not m or m <= 0 or m > d["saldo"]: flash("Monto inválido o mayor al saldo")
    else:
        igtf = money(m * IGTF / 100) if request.form.get("divisas") else D(0)
        ex("insert into pagos(doc_id,fecha,monto,igtf,ref,usuario) values(%s,%s,%s,%s,%s,%s)",
           (did, dfecha(request.form.get("fecha")), m, igtf, request.form["ref"].strip(), session["usuario"]))
        ex("update docs set saldo=saldo-%s where id=%s", (m, did)); db().commit()
    return redirect(url_for("cuentas", tipo="cobrar" if d["tipo"] == "venta" else "pagar"))

# ---------- ISLR e IGTF ----------
@app.route("/impuestos", methods=["GET", "POST"])
@login_required("admin")
def impuestos():
    if request.method == "POST":
        f = request.form
        if f["accion"] == "concepto":
            p = num(f["porcentaje"])
            if p is None or not (0 <= p <= 100) or not f["codigo"].strip(): flash("Concepto inválido")
            else:
                try:
                    ex("insert into islr_conceptos(codigo,descripcion,porcentaje) values(%s,%s,%s)", (f["codigo"].strip(), f["descripcion"].strip(), p)); db().commit()
                except pgerr.UniqueViolation:
                    db().rollback(); flash("Código duplicado")
        elif f["accion"] == "estado":
            ex("update islr_conceptos set activo = not activo where id=%s", (f["id"],)); db().commit()
        return redirect(url_for("impuestos"))
    mes = request.args.get("mes") or date.today().strftime("%Y-%m"); d1, d2 = rango(mes)
    return render_template_string(T_IMP, mes=mes,
        conceptos=q("select * from islr_conceptos order by codigo"),
        islr=q("select * from docs where islr_monto<>0 and fecha>=%s and fecha<%s order by fecha", (d1, d2)),
        igtf_docs=q("select * from docs where igtf>0 and fecha>=%s and fecha<%s order by fecha", (d1, d2)),
        igtf_pagos=q("select p.*,d.tipo,d.rif,d.nro_factura from pagos p join docs d on d.id=p.doc_id where p.igtf>0 and p.fecha>=%s and p.fecha<%s order by p.fecha", (d1, d2)))

# ---------- Plantillas ----------
T = {"base": """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name=theme-color content="#07130f"><title>Sistema Contable VE</title>
<style>
:root{--ink:#0b0f0e;--dark:#07130f;--bg:#f6f6f2;--card:#fff;--line:#e3e3dc;--mut:#6b726e;--green:#008060;--mint:#c9f4dc;--ease:cubic-bezier(.22,.8,.3,1)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Helvetica Neue",Inter,system-ui,-apple-system,"Segoe UI",sans-serif;letter-spacing:-.005em;-webkit-font-smoothing:antialiased}
nav{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:12px 28px;background:rgba(7,19,15,.88);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
nav a{position:relative;color:#e9efec;text-decoration:none;font-size:14px;padding:6px 10px;border-radius:999px;transition:color .25s,background .25s}
nav a:not(.brand):not(.pill)::after{content:"";position:absolute;left:10px;right:10px;bottom:2px;height:1px;background:var(--mint);transform:scaleX(0);transform-origin:left;transition:transform .35s var(--ease)}
nav a:not(.brand):not(.pill):hover::after{transform:scaleX(1)}
nav a:hover{color:#fff}
nav .brand{font-weight:600;font-size:18px;letter-spacing:-.03em;margin-right:14px;color:#fff}
nav .brand span{color:var(--mint)}
nav .sp{flex:1}
nav a.pill{background:#fff;color:var(--ink);padding:7px 16px;transition:background .25s,transform .25s var(--ease)}
nav a.pill:hover{background:var(--mint);color:var(--ink);transform:translateY(-1px)}
main{padding:32px 28px 64px;max-width:1400px;margin:auto;overflow-x:auto;animation:rise .6s var(--ease) both}
h3{font-size:30px;font-weight:300;letter-spacing:-.035em;margin:8px 0 18px}
h3 a{margin-left:10px;vertical-align:middle}
.box,form.box{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:22px;margin-bottom:22px;transition:box-shadow .35s var(--ease),transform .35s var(--ease)}
.box:hover{box-shadow:0 18px 40px -22px rgba(7,19,15,.35);transform:translateY(-2px)}
table{width:100%;border-collapse:separate;border-spacing:0;background:var(--card);border:1px solid var(--line);border-radius:18px;overflow:hidden;margin:6px 0 24px}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}
tr:last-child td,tr:last-child th{border-bottom:0}
th{background:#fafaf7;font-size:11.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
tbody tr,table>tr{transition:background .2s}
tr:hover td{background:#f4faf6}
tr{animation:rise .5s var(--ease) both}
tr:nth-child(2){animation-delay:.04s}tr:nth-child(3){animation-delay:.08s}tr:nth-child(4){animation-delay:.12s}tr:nth-child(5){animation-delay:.16s}tr:nth-child(6){animation-delay:.2s}tr:nth-child(n+7){animation-delay:.24s}
.r{text-align:right;font-variant-numeric:tabular-nums}
input,select{font:inherit;padding:9px 13px;margin:3px;border:1px solid var(--line);border-radius:12px;background:#fff;transition:border-color .2s,box-shadow .25s}
input:hover,select:hover{border-color:#c5c9c4}
input:focus,select:focus{outline:0;border-color:var(--green);box-shadow:0 0 0 4px rgba(0,128,96,.14)}
input[type=checkbox]{accent-color:var(--green);margin:0 6px 0 4px}
button{font:inherit;font-weight:500;padding:10px 20px;margin:3px;border:0;border-radius:999px;background:var(--ink);color:#fff;cursor:pointer;transition:background .25s,transform .25s var(--ease),box-shadow .25s}
button:hover{background:#1f3a30;transform:translateY(-1px);box-shadow:0 10px 20px -12px rgba(7,19,15,.6)}
button:active{transform:scale(.97)}
a{color:var(--green);transition:color .2s}a:hover{color:#005a43}
.f{background:var(--mint);color:#0b3b2a;padding:12px 18px;margin:0 0 18px;border-radius:14px;animation:slide .45s var(--ease) both}
.mal{color:#b3261e;font-weight:500}
.hero{position:relative;overflow:hidden;background:var(--dark);color:#fff;border-radius:28px;padding:56px 44px;margin:8px 0 8px;display:grid;grid-template-columns:1.2fr 1fr;gap:32px;align-items:center}
.hero::before{content:"";position:absolute;width:520px;height:520px;right:-120px;top:-160px;background:radial-gradient(circle,rgba(120,235,176,.55),transparent 65%);animation:glow 9s ease-in-out infinite alternate;pointer-events:none}
.hero h1{position:relative;font-size:clamp(34px,5vw,60px);font-weight:300;letter-spacing:-.045em;line-height:1.02;margin:0 0 14px}
.hero h1 em{font-style:normal;color:var(--mint)}
.hero p{position:relative;color:#b9c7c0;margin:0;max-width:34ch}
.hero .box{position:relative;color:var(--ink);margin:0}
.hero .box:hover{transform:none}
.hero button{width:100%;margin:8px 0 0;padding:12px}
.hero input{width:100%;margin:4px 0}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes slide{from{opacity:0;transform:translateX(-14px)}to{opacity:1;transform:none}}
@keyframes glow{from{transform:translate(0,0) scale(1)}to{transform:translate(-60px,50px) scale(1.15)}}
.demo{background:var(--mint);color:#0b3b2a;text-align:center;padding:7px 12px;font-size:13px}#mn{display:none}.burger{display:none}.links{display:flex;flex:1;align-items:center;gap:6px;flex-wrap:wrap}
body{-webkit-tap-highlight-color:transparent}button,input,select,a,label{touch-action:manipulation}
@media(hover:none){.box:hover,button:hover,nav a.pill:hover{transform:none;box-shadow:none}tr:hover td{background:inherit}}
@media(max-width:860px){
nav{padding:10px 16px;padding-top:calc(10px + env(safe-area-inset-top));padding-left:calc(16px + env(safe-area-inset-left));padding-right:calc(16px + env(safe-area-inset-right))}
nav .brand{margin-right:0}
.burger{display:flex;align-items:center;justify-content:center;width:44px;height:44px;margin-left:auto;cursor:pointer;border-radius:999px;transition:background .25s}
.burger:active{background:rgba(255,255,255,.12)}
.burger i,.burger i::before,.burger i::after{display:block;width:20px;height:2px;background:#fff;border-radius:2px;position:relative;transition:transform .3s var(--ease),background .2s,top .3s var(--ease)}
.burger i::before,.burger i::after{content:"";position:absolute;left:0}
.burger i::before{top:-6px}.burger i::after{top:6px}
#mn:checked~.burger i{background:transparent}
#mn:checked~.burger i::before{top:0;transform:rotate(45deg)}
#mn:checked~.burger i::after{top:0;transform:rotate(-45deg)}
.links{display:none;flex-basis:100%;flex-direction:column;align-items:stretch;gap:2px;padding:8px 0 6px}
#mn:checked~.links{display:flex;animation:rise .3s var(--ease) both}
nav .links a{font-size:16px;padding:13px 14px;border-radius:14px}
nav .links a:active{background:rgba(255,255,255,.1)}
nav a:not(.brand):not(.pill)::after{display:none}
nav .sp{display:none}
nav a.pill{margin-top:8px;text-align:center;padding:13px 16px}
main{padding:20px 14px 48px;padding-left:calc(14px + env(safe-area-inset-left));padding-right:calc(14px + env(safe-area-inset-right));padding-bottom:calc(48px + env(safe-area-inset-bottom));overflow-x:hidden}
h3{font-size:24px;margin:4px 0 14px}
h3 a{display:block;margin:10px 0 0}h3 a button{width:100%}
.box,form.box{padding:16px;border-radius:18px}
input,select{font-size:16px;min-height:44px;width:100%;margin:4px 0}
input[type=checkbox]{width:20px;min-height:20px;height:20px;vertical-align:middle}
label{display:block;padding:6px 0}
button{font-size:16px;min-height:44px;padding:10px 20px}
.box>form>button,form.box>button{width:100%;margin-top:10px}
table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch;white-space:nowrap;border-radius:14px}
th,td{padding:12px 12px}
td input,td select{width:auto;min-width:120px}
td form{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
td button{width:auto;min-height:40px;padding:8px 16px}
table.lin,table.lin tbody{display:block;overflow:visible;white-space:normal;border:0;background:transparent}
table.lin tr{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:12px;margin-bottom:10px;background:#fff;border:1px solid var(--line);border-radius:16px}
table.lin tr:first-child{display:none}
table.lin td{display:block;border:0;padding:0}
table.lin td:first-child{grid-column:1/-1}
table.lin td input,table.lin td select{width:100%;min-width:0;margin:0}
.hero{grid-template-columns:1fr;padding:30px 20px;border-radius:22px;gap:22px}
.hero::before{width:320px;height:320px;right:-120px;top:-120px}
.f{padding:12px 14px}
}
@media(max-width:380px){nav .brand{font-size:16px}h3{font-size:21px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style>
{% if DEMO %}<div class=demo>Versión de demostración · todos los datos son ficticios</div>{% endif %}
<nav><a class=brand href="/">Sistema<span>VE</span></a>{% if session.rol %}<input type=checkbox id=mn><label for=mn class=burger aria-label=Menú><i></i></label><div class=links>
{% if session.rol=='admin' %}<a href="/doc/venta">Ventas</a><a href="/doc/compra">Compras</a><a href="/libro/venta">Libro ventas</a><a href="/libro/compra">Libro compras</a><a href="/cuentas/cobrar">CxC</a><a href="/cuentas/pagar">CxP</a><a href="/impuestos">ISLR/IGTF</a><a href="/productos">Inventario</a>{% endif %}
{% if session.rol=='inventario' %}<a href="/productos">Inventario</a>{% endif %}
{% if session.rol=='usuarios' %}<a href="/usuarios">Usuarios</a>{% endif %}
<span class=sp></span><a href="/clave">Mi clave</a><a class=pill href="/logout">Salir ({{session.usuario}})</a></div>{% endif %}</nav>
<main>{% for m in get_flashed_messages() %}<div class="f">{{m}}</div>{% endfor %}{% block c %}{% endblock %}</main>"""}
app.jinja_loader = DictLoader(T)
H = '{% extends "base" %}{% block c %}'; E = "{% endblock %}"
T_LOGIN = H + '''<section class=hero><div><h1>Tu negocio,<br><em>en orden.</em></h1><p>Inventario, libros de compras y ventas, cuentas por cobrar y por pagar en un solo lugar.</p></div>
<div class=box><h3>Ingresar</h3><form method=post>{{T|safe}}<input name=usuario placeholder=Usuario required autofocus autocomplete=username autocapitalize=off><input name=clave type=password placeholder=Clave required autocomplete=current-password><button>Entrar</button></form>{% if DEMO_CREDS %}<p style="font-size:13px;color:#6b726e;margin:14px 0 0">Accesos de la demo:{% for u,p in DEMO_CREDS %}<br><b>{{u}}</b> / {{p}}{% endfor %}</p>{% endif %}</div></section>''' + E
T_CLAVE = H + '<div class=box><h3>Cambiar clave</h3><form method=post>{{T|safe}}<input type=password name=actual placeholder="Clave actual" required><input type=password name=nueva placeholder="Nueva (mín. 8)" required><button>Guardar</button></form></div>' + E
T_USUARIOS = H + '''<div class=box><h3>Nuevo usuario</h3><form method=post>{{T|safe}}<input type=hidden name=accion value=crear>
<input name=usuario placeholder=Usuario required><input name=nombre placeholder=Nombre><input name=clave type=password placeholder="Clave (mín. 8)" required>
<select name=rol>{% for k,v in ROLES.items() %}<option value={{k}}>{{v}}</option>{% endfor %}</select><button>Crear</button></form></div>
<table><tr><th>Usuario<th>Nombre<th>Rol<th>Estado<th>Acciones</tr>{% for u in users %}<tr><td>{{u.usuario}}<td>{{u.nombre}}<td>{{ROLES[u.rol]}}<td>{{'Activo' if u.activo else 'Inactivo'}}
<td><form method=post style=display:inline>{{T|safe}}<input type=hidden name=accion value=estado><input type=hidden name=id value={{u.id}}><button>{{'Desactivar' if u.activo else 'Activar'}}</button></form>
<form method=post style=display:inline>{{T|safe}}<input type=hidden name=accion value=reset><input type=hidden name=id value={{u.id}}><input type=password name=clave placeholder="Nueva clave" minlength=8 required><button>Restablecer</button></form></tr>{% endfor %}</table>''' + E
T_PROD = H + '''{% if session.rol=='inventario' %}<div class=box><h3>Nuevo producto</h3><form method=post>{{T|safe}}<input type=hidden name=accion value=crear>
<input name=codigo placeholder=Código required><input name=descripcion placeholder=Descripción required><input name=unidad placeholder=Unidad size=6>
<input name=costo placeholder=Costo size=8 inputmode=decimal><input name=precio placeholder="Precio venta" size=8 inputmode=decimal><select name=alicuota><option value=G>IVA 16%</option><option value=R>IVA 8%</option><option value=L>Lujo 31%</option><option value=E>Exento</option></select>
<input name=minimo placeholder="Mín." size=5 inputmode=decimal><button>Crear</button></form></div>{% endif %}
<table><tr><th>Código<th>Descripción<th>Und<th class=r>Stock<th class=r>Costo<th class=r>Precio<th>IVA<th>Ajuste manual</tr>
{% for p in prods %}<tr><td>{{p.codigo}}<td>{{p.descripcion}}<td>{{p.unidad}}<td class=r {% if p.stock<=p.minimo %}style=color:#b00020{% endif %}>{{p.stock|qty}}<td class=r>{{p.costo|bs}}<td class=r>{{p.precio|bs}}<td>{{p.alicuota}}
<td>{% if session.rol=='inventario' %}<form method=post>{{T|safe}}<input type=hidden name=accion value=ajuste><input type=hidden name=id value={{p.id}}><select name=tipo><option>entrada<option>salida</select><input name=cantidad size=5 placeholder=Cant inputmode=decimal><input name=motivo placeholder=Motivo><button>OK</button></form>{% endif %}</tr>{% endfor %}</table>
<h3>Últimos movimientos</h3><table><tr><th>Fecha<th>Producto<th>Tipo<th class=r>Cant<th>Ref<th>Usuario</tr>{% for m in movs %}<tr><td>{{m.fecha}}<td>{{m.descripcion}}<td>{{m.tipo}}<td class=r>{{m.cantidad|qty}}<td>{{m.ref}}<td>{{m.usuario}}</tr>{% endfor %}</table>''' + E
T_DOCS = H + '''<h3>{{'Ventas' if tipo=='venta' else 'Compras'}} <a href="/doc/{{tipo}}/nuevo"><button>+ Registrar</button></a></h3>
<table><tr><th>Fecha<th>Tipo<th>RIF<th>Nombre<th>Nº<th>Control<th class=r>Total<th class=r>IVA ret.<th class=r>ISLR ret.<th class=r>IGTF<th>Cond.<th class=r>Saldo</tr>{% for d in docs %}<tr><td>{{d.fecha}}<td>{{d.doc_tipo}}<td>{{d.rif}}<td>{{d.nombre}}<td>{{d.nro_factura}}<td>{{d.nro_control}}<td class=r>{{d.total|bs}}<td class=r>{{d.retencion|bs}}<td class=r>{{d.islr_monto|bs}}<td class=r>{{d.igtf|bs}}<td>{{d.condicion}}<td class=r>{{d.saldo|bs}}</tr>{% endfor %}</table>''' + E
T_FORM = H + '''<h3>Registrar {{tipo}}</h3><form method=post class=box>{{T|safe}}
<select name=doc_tipo>{% for k,v in DOCT.items() %}<option value={{k}}>{{v}}</option>{% endfor %}</select><input name=nro_afectada placeholder="Factura afectada (ND/NC)">
<input type=date name=fecha value={{hoy}}><input name=rif placeholder="RIF {{'cliente' if tipo=='venta' else 'proveedor'}}" required><input name=nombre placeholder="Razón social" required size=28>
<input name=nro_factura placeholder="Nº documento" required><input name=nro_control placeholder="Nº control" required><br>
Condición <select name=condicion><option value=contado>Contado<option value=credito>Crédito</select> Días <input name=dias value=30 size=3 inputmode=numeric>
Retención IVA <select name=ret_pct><option value=0>No<option value=75>75%<option value=100>100%</select>
Retención ISLR <select name=islr><option value="">No</option>{% for c in conceptos %}<option value={{c.id}}>{{c.codigo}} ({{c.porcentaje}}%)</option>{% endfor %}</select>
<label><input type=checkbox name=divisas> Pago en divisas (IGTF 3%, contado)</label>
<table class=lin><tr><th>Producto<th>Cantidad<th>Precio unit. (Bs, sin IVA)</tr>{% for i in range(8) %}<tr><td><select name=p{{i}}><option value="">—</option>{% for p in prods %}<option value={{p.id}}>{{p.codigo}} · {{p.descripcion}} (IVA {{p.alicuota}})</option>{% endfor %}</select><td><input name=c{{i}} size=6 inputmode=decimal placeholder=Cantidad><td><input name=pr{{i}} size=10 inputmode=decimal placeholder=Precio></tr>{% endfor %}</table>
<button>Guardar</button></form>''' + E
T_LIBRO = H + '''<h3>Libro de {{'ventas' if tipo=='venta' else 'compras'}} — {{mes}}</h3>
<form>Mes <input type=month name=mes value={{mes}}><button>Ver</button> <a href="/libro/{{tipo}}.csv?mes={{mes}}">Exportar CSV</a></form>
<table><tr><th>Fecha<th>Tipo<th>RIF<th>Razón social<th>Nº doc.<th>Control<th>Afecta a<th class=r>Exento<th class=r>Base 16%<th class=r>Base 8%<th class=r>Base lujo<th class=r>IVA 16%<th class=r>IVA 8%<th class=r>IVA lujo<th class=r>Total<th class=r>IVA retenido</tr>
{% for r in rows %}<tr><td>{{r.fecha}}<td>{{r.doc_tipo}}<td>{{r.rif}}<td>{{r.nombre}}<td>{{r.nro_factura}}<td>{{r.nro_control}}<td>{{r.nro_afectada or ''}}<td class=r>{{r.base_exenta|bs}}<td class=r>{{r.base_general|bs}}<td class=r>{{r.base_reducida|bs}}<td class=r>{{r.base_lujo|bs}}<td class=r>{{r.iva_general|bs}}<td class=r>{{r.iva_reducida|bs}}<td class=r>{{r.iva_lujo|bs}}<td class=r>{{r.total|bs}}<td class=r>{{r.retencion|bs}}</tr>{% endfor %}
<tr><th colspan=7>TOTALES<th class=r>{{tot.base_exenta|bs}}<th class=r>{{tot.base_general|bs}}<th class=r>{{tot.base_reducida|bs}}<th class=r>{{tot.base_lujo|bs}}<th class=r>{{tot.iva_general|bs}}<th class=r>{{tot.iva_reducida|bs}}<th class=r>{{tot.iva_lujo|bs}}<th class=r>{{tot.total|bs}}<th class=r>{{tot.retencion|bs}}</tr></table>''' + E
T_CTAS = H + '''<h3>Cuentas por {{tipo}} — saldo total Bs {{total|bs}}</h3>
<table><tr><th>Tipo<th>Nº<th>RIF<th>Nombre<th>Emisión<th>Vence<th class=r>Saldo<th>Registrar pago</tr>{% for r in rows %}<tr><td>{{r.doc_tipo}}<td>{{r.nro_factura}}<td>{{r.rif}}<td>{{r.nombre}}<td>{{r.fecha}}<td {% if r.vence<hoy %}class=mal{% endif %}>{{r.vence}}<td class=r>{{r.saldo|bs}}
<td><form method=post action="/pagar/{{r.id}}">{{T|safe}}<input type=date name=fecha value={{hoy}}><input name=monto size=9 placeholder=Monto inputmode=decimal><input name=ref placeholder=Referencia><label><input type=checkbox name=divisas>Divisas (IGTF)</label><button>Abonar</button></form></tr>{% endfor %}</table>''' + E
T_IMP = H + '''<div class=box><h3>Conceptos de retención ISLR (Decreto 1.808; cargue porcentajes vigentes)</h3><form method=post>{{T|safe}}<input type=hidden name=accion value=concepto>
<input name=codigo placeholder=Código required><input name=descripcion placeholder=Descripción size=30><input name=porcentaje placeholder="%" size=5 required inputmode=decimal><button>Agregar</button></form>
<table><tr><th>Código<th>Descripción<th class=r>%<th>Estado</tr>{% for c in conceptos %}<tr><td>{{c.codigo}}<td>{{c.descripcion}}<td class=r>{{c.porcentaje}}<td><form method=post>{{T|safe}}<input type=hidden name=accion value=estado><input type=hidden name=id value={{c.id}}><button>{{'Activo' if c.activo else 'Inactivo'}}</button></form></tr>{% endfor %}</table></div>
<form>Mes <input type=month name=mes value={{mes}}><button>Ver</button></form>
<h3>Retenciones ISLR — {{mes}}</h3><table><tr><th>Fecha<th>Operación<th>Doc.<th>RIF<th>Nº<th class=r>%<th class=r>Retenido</tr>{% for d in islr %}<tr><td>{{d.fecha}}<td>{{d.tipo}}<td>{{d.doc_tipo}}<td>{{d.rif}}<td>{{d.nro_factura}}<td class=r>{{d.islr_pct}}<td class=r>{{d.islr_monto|bs}}</tr>{% endfor %}
<tr><th colspan=6>Total<th class=r>{{islr|sum(attribute='islr_monto')|bs}}</tr></table>
<h3>IGTF 3% — {{mes}}</h3><table><tr><th>Fecha<th>Operación<th>RIF<th>Nº<th>Origen<th class=r>IGTF</tr>
{% for d in igtf_docs %}<tr><td>{{d.fecha}}<td>{{d.tipo}}<td>{{d.rif}}<td>{{d.nro_factura}}<td>Contado<td class=r>{{d.igtf|bs}}</tr>{% endfor %}
{% for p in igtf_pagos %}<tr><td>{{p.fecha}}<td>{{p.tipo}}<td>{{p.rif}}<td>{{p.nro_factura}}<td>Abono<td class=r>{{p.igtf|bs}}</tr>{% endfor %}
<tr><th colspan=5>Total<th class=r>{{((igtf_docs|sum(attribute='igtf'))+(igtf_pagos|sum(attribute='igtf')))|bs}}</tr></table>''' + E

if __name__ == "__main__":
    import sys
    init_db()
    if "--reset-demo" in sys.argv:       # borra los datos y vuelve a cargar los de ejemplo
        reset_demo(); print("Demo restablecida"); sys.exit()
    if "--reset-claves" in sys.argv:   # regenera y muestra las claves de los 3 usuarios base
        with psycopg.connect(DSN) as c:
            for u in ("admin", "inventario", "usuarios"):
                pw = os.environ.get(f"PASS_{u.upper()}") or secrets.token_urlsafe(9)
                c.execute("update users set clave=%s, activo=true where usuario=%s", (generate_password_hash(pw), u))
                print(f"{u} / {pw}")
        sys.exit()
    app.run(debug=False)
else:
    init_db()