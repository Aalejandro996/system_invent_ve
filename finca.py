# finca.py — módulos de finca: ganado, potreros, traslados, sanidad, alimentación, reproducción y personal.
# Todo se define en ENT (una entrada por módulo); agregar un módulo nuevo = agregar una entrada.
from datetime import date, datetime, timedelta, time as dtime
from decimal import Decimal as D
from zoneinfo import ZoneInfo
from flask import request, session, redirect, flash, render_template_string
from werkzeug.exceptions import abort
from psycopg import errors as pgerr

q = ex = db = num = rango = xlsx = None      # los inyecta register() desde app.py
TZ = ZoneInfo("America/Caracas")
def hoy(): return datetime.now(TZ).date()
def C(n, l, t="text", o=None, req=False): return dict(n=n, l=l, t=t, o=o, req=req)

CATEG = ["Becerro", "Becerra", "Maute", "Mauta", "Novillo", "Novilla", "Torete", "Vaca", "Toro"]
ENT = {
 "potreros": dict(t="Potreros", r="finca", lbl="nombre", uniq=["nombre"], cols=[C("nombre", "Nombre", req=True),
    C("hectareas", "Hectáreas", "num"), C("capacidad", "Capacidad (cabezas)", "num"), C("pasto", "Pasto / forraje"),
    C("lat", "Latitud", "num"), C("lon", "Longitud", "num"), C("notas", "Notas")]),
 "animales": dict(t="Ganado", r="finca", lbl="arete", uniq=["arete"], cols=[C("arete", "Arete / ID", req=True), C("nombre", "Nombre"),
    C("especie", "Especie", "sel", ["Bovino", "Bufalino", "Equino", "Porcino", "Ovino", "Caprino"]), C("raza", "Raza"),
    C("sexo", "Sexo", "sel", ["Macho", "Hembra"], True), C("nacimiento", "Nacimiento", "date"), C("categoria", "Categoría", "sel", CATEG),
    C("peso_kg", "Peso (kg)", "num"), C("potrero_id", "Ubicación (potrero)", "fk", "potreros"), C("lote", "Lote"),
    C("estado", "Estado", "sel", ["Activo", "Vendido", "Muerto", "Trasladado"], True), C("madre_arete", "Arete madre"),
    C("padre_arete", "Arete padre"), C("notas", "Notas")]),
 "pesajes": dict(t="Pesajes", r="finca", cols=[C("animal_id", "Animal", "fk", "animales", True), C("fecha", "Fecha", "date", None, True),
    C("peso_kg", "Peso (kg)", "num", None, True), C("notas", "Notas")]),
 "traslados": dict(t="Traslados", r="finca", cols=[C("fecha", "Fecha", "date", None, True),
    C("tipo", "Tipo", "sel", ["Interno entre potreros", "Transporte externo", "Transbordo"], True), C("animal_id", "Animal", "fk", "animales"),
    C("lote", "Lote / grupo"), C("cantidad", "Cantidad de cabezas", "num"), C("origen_potrero_id", "Origen (potrero)", "fk", "potreros"),
    C("destino_potrero_id", "Destino (potrero)", "fk", "potreros"), C("destino_texto", "Destino externo"), C("vehiculo", "Placa del vehículo"),
    C("chofer", "Chofer"), C("guia", "Nº guía de movilización"), C("estado", "Estado", "sel", ["Programado", "En tránsito", "Entregado"], True),
    C("notas", "Notas")]),
 "sanidad": dict(t="Sanidad", r="finca", alert="proxima_fecha", cols=[C("fecha", "Fecha", "date", None, True),
    C("tipo", "Tipo", "sel", ["Vacunación", "Desparasitación", "Tratamiento", "Vitaminas", "Otro"], True), C("producto", "Producto / vacuna", req=True),
    C("animal_id", "Animal", "fk", "animales"), C("lote", "Lote / grupo"), C("dosis", "Dosis"), C("responsable", "Responsable"),
    C("proxima_fecha", "Próxima aplicación", "date"), C("costo", "Costo", "num"), C("notas", "Notas")]),
 "insumos": dict(t="Insumos", r="finca", lbl="nombre", uniq=["nombre"], low=("stock", "minimo"), cols=[C("nombre", "Nombre", req=True),
    C("tipo", "Tipo", "sel", ["Alimento", "Mineral/Sal", "Medicina", "Combustible", "Otro"]), C("unidad", "Unidad", req=True),
    C("stock", "Existencia", "num"), C("minimo", "Mínimo", "num"), C("costo_unit", "Costo unitario", "num"), C("proveedor", "Proveedor")]),
 "consumos": dict(t="Alimentación", r="finca", cols=[C("fecha", "Fecha", "date", None, True), C("insumo_id", "Insumo", "fk", "insumos", True),
    C("cantidad", "Cantidad", "num", None, True), C("potrero_id", "Potrero", "fk", "potreros"), C("lote", "Lote / grupo"), C("notas", "Notas")]),
 "entradas": dict(t="Entradas de insumos", r="finca", cols=[C("fecha", "Fecha", "date", None, True), C("insumo_id", "Insumo", "fk", "insumos", True),
    C("cantidad", "Cantidad", "num", None, True), C("costo_total", "Costo total", "num"), C("proveedor", "Proveedor"), C("factura", "Nº factura")]),
 "reproduccion": dict(t="Reproducción", r="finca", alert="fecha_parto_estimada", cols=[C("fecha", "Fecha", "date", None, True),
    C("tipo", "Evento", "sel", ["Monta", "Inseminación", "Diagnóstico de preñez", "Parto", "Destete"], True), C("hembra_id", "Hembra", "fk", "animales", True),
    C("macho_id", "Macho / semental", "fk", "animales"), C("resultado", "Resultado"), C("fecha_parto_estimada", "Parto estimado", "date"),
    C("crias", "Crías", "int"), C("notas", "Notas")]),
 "puestos": dict(t="Puestos", r="personal", lbl="nombre", uniq=["nombre"], cols=[C("nombre", "Puesto", req=True), C("descripcion", "Descripción"),
    C("hora_entrada", "Hora de entrada", "time", None, True), C("hora_salida", "Hora de salida", "time")]),
 "empleados": dict(t="Empleados", r="personal", lbl="nombre", uniq=["cedula"], cols=[C("nombre", "Nombre", req=True), C("cedula", "Cédula", req=True),
    C("puesto_id", "Puesto", "fk", "puestos"), C("telefono", "Teléfono"), C("fecha_ingreso", "Fecha de ingreso", "date"),
    C("estado", "Estado", "sel", ["Activo", "Inactivo"], True), C("notas", "Notas")]),
 "asistencia": dict(t="Asistencia", r="personal", uniq=[("fecha", "empleado_id")], cols=[C("fecha", "Fecha", "date", None, True),
    C("empleado_id", "Empleado", "fk", "empleados", True), C("hora_llegada", "Hora de llegada", "time"), C("hora_salida", "Hora de salida", "time"),
    C("notas", "Notas")]),
}
TY = {"text": "text", "num": "numeric(18,4)", "int": "int", "date": "date", "time": "time", "sel": "text"}

def ddl():
    out = []
    for k, e in ENT.items():
        cols = [f'{c["n"]} ' + (f'int references f_{c["o"]}(id)' if c["t"] == "fk" else TY[c["t"]]) for c in e["cols"]]
        u = "".join(", unique(" + ",".join(x if isinstance(x, tuple) else (x,)) + ")" for x in e.get("uniq", []))
        out.append(f"create table if not exists f_{k}(id serial primary key, {', '.join(cols)}, usuario text, creado timestamptz default now(){u})")
    return ";\n".join(out)

def nav(rol):
    if rol == "finca": return [("/finca", "Resumen")] + [(f"/f/{k}", e["t"]) for k, e in ENT.items() if e["r"] == "finca"]
    if rol == "personal": return [(f"/f/{k}", e["t"]) for k, e in ENT.items() if e["r"] == "personal"] + [("/personal/reporte", "Reporte")]
    if rol == "admin": return [("/finca", "Finca"), ("/f/asistencia", "Personal")]
    return []

# ---------- efectos al crear registros ----------
def hk_pesajes(v):
    if v["peso_kg"] <= 0: raise ValueError("El peso debe ser mayor a 0")
    ex("update f_animales set peso_kg=%s where id=%s", (v["peso_kg"], v["animal_id"]))
def hk_traslados(v):
    if v["animal_id"] and v["destino_potrero_id"] and v["estado"] == "Entregado":
        ex("update f_animales set potrero_id=%s where id=%s", (v["destino_potrero_id"], v["animal_id"]))
def hk_consumos(v):
    if v["cantidad"] <= 0: raise ValueError("La cantidad debe ser mayor a 0")
    i = q("select * from f_insumos where id=%s", (v["insumo_id"],), one=True)
    if i["stock"] < v["cantidad"]: raise ValueError(f"Existencia insuficiente de {i['nombre']} (disp. {i['stock']:g})")
    ex("update f_insumos set stock=stock-%s where id=%s", (v["cantidad"], i["id"]))
def hk_entradas(v):
    if v["cantidad"] <= 0: raise ValueError("La cantidad debe ser mayor a 0")
    ex("update f_insumos set stock=stock+%s where id=%s", (v["cantidad"], v["insumo_id"]))
    if v["costo_total"]: ex("update f_insumos set costo_unit=%s where id=%s", ((v["costo_total"] / v["cantidad"]).quantize(D("0.0001")), v["insumo_id"]))
HOOKS = {"pesajes": hk_pesajes, "traslados": hk_traslados, "consumos": hk_consumos, "entradas": hk_entradas}

# ---------- utilidades ----------
def ent(k):
    e = ENT.get(k) or abort(404)
    if "uid" not in session: abort(redirect("/login"))
    if session["rol"] not in (e["r"], "admin"): abort(403)
    return e

def coerce(c, raw):
    raw = (raw or "").strip()
    if not raw: return None
    t = c["t"]
    try:
        if t == "num": return num(raw)
        if t in ("int", "fk"): return int(raw) if raw.isdigit() else None
        if t == "date": return date.fromisoformat(raw)
        if t == "time": return datetime.strptime(raw[:5], "%H:%M").time()
        if t == "sel": return raw if raw in c["o"] else None
        return raw[:300]
    except ValueError:
        return None

def sval(v):
    if v is None: return ""
    if isinstance(v, (date, dtime)): return v.isoformat()[:10] if isinstance(v, date) else v.strftime("%H:%M")
    return format(v.normalize(), "f") if isinstance(v, D) else str(v)

def fetch(k, where="", args=(), limit=500):
    sel, join = ["t.id"], []
    for i, c in enumerate(ENT[k]["cols"]):
        if c["t"] == "fk":
            join.append(f"left join f_{c['o']} j{i} on j{i}.id=t.{c['n']}"); sel.append(f"j{i}.{ENT[c['o']]['lbl']} as {c['n']}")
        else: sel.append(f"t.{c['n']}")
    return q(f"select {','.join(sel)} from f_{k} t {' '.join(join)} {where} order by t.id desc limit {limit}", args)

# ---------- rutas genéricas ----------
def f_list(k):
    e, s = ent(k), request.args.get("q", "").strip()
    tc, where, args = [c["n"] for c in e["cols"] if c["t"] == "text"], "", ()
    if s and tc: where, args = "where " + " or ".join(f"t.{n} ilike %s" for n in tc), tuple(f"%{s}%" for _ in tc)
    emp = q("select id,nombre from f_empleados where estado='Activo' order by nombre") if k == "asistencia" else []
    return render_template_string(T_LIST, k=k, e=e, rows=fetch(k, where, args), q=s, hoy=hoy(), emp=emp)

def f_form(k, id=None):
    e, cols = ent(k), None
    cols = e["cols"]
    r = q(f"select * from f_{k} where id=%s", (id,), one=True) if id else None
    if id and not r: abort(404)
    opts = {c["n"]: [(str(x["id"]), x["l"]) for x in q(f"select id, {ENT[c['o']]['lbl']} as l from f_{c['o']} order by l")] for c in cols if c["t"] == "fk"}
    if request.method == "POST":
        sv, vals, err = {c["n"]: request.form.get(c["n"], "") for c in cols}, {}, None
        for c in cols:
            vals[c["n"]] = coerce(c, sv[c["n"]])
            if vals[c["n"]] is None and c["req"]: err = err or f"Falta: {c['l']}"
        try:
            if not err and not id and k in HOOKS: HOOKS[k](vals)
            if not err:
                cn = [c["n"] for c in cols]
                if id: ex(f"update f_{k} set {', '.join(n + '=%s' for n in cn)} where id=%s", [vals[n] for n in cn] + [id])
                else: ex(f"insert into f_{k}({','.join(cn)},usuario) values({','.join(['%s'] * (len(cn) + 1))})", [vals[n] for n in cn] + [session["usuario"]])
                db().commit(); return redirect(f"/f/{k}")
        except ValueError as x: err = str(x)
        except pgerr.UniqueViolation: err = "Ya existe un registro igual (valor único repetido)"
        except pgerr.ForeignKeyViolation: err = "Referencia inválida"
        db().rollback(); flash(err)
    else:
        sv = {c["n"]: sval(r[c["n"]]) if r else ("" if c["t"] != "date" or c["n"] != "fecha" else hoy().isoformat()) for c in cols}
        if not r:
            for c in cols:
                if c["t"] == "sel" and c["req"] and not sv[c["n"]]: sv[c["n"]] = c["o"][0]
    return render_template_string(T_FORM, k=k, e=e, r=r, sv=sv, opts=opts)

def f_del(k, id):
    e = ent(k)
    if request.method == "POST":
        try: ex(f"delete from f_{k} where id=%s", (id,)); db().commit()
        except pgerr.ForeignKeyViolation: db().rollback(); flash("No se puede borrar: el registro está en uso en otros módulos")
        return redirect(f"/f/{k}")
    return render_template_string(T_DEL, k=k, e=e)

def f_xlsx(k):
    e = ent(k)
    return xlsx(k, ["ID"] + [c["l"] for c in e["cols"]], [[r["id"]] + [r[c["n"]] for c in e["cols"]] for r in fetch(k, limit=100000)])

# ---------- resumen de finca ----------
def f_home():
    if "uid" not in session: abort(redirect("/login"))
    if session["rol"] not in ("finca", "admin"): abort(403)
    t = hoy()
    d = dict(
      total=q("select count(*) n from f_animales where estado='Activo'", one=True)["n"],
      cat=q("select coalesce(categoria,'Sin categoría') n, count(*) c from f_animales where estado='Activo' group by 1 order by 2 desc"),
      pot=q("select coalesce(p.nombre,'Sin asignar') n, count(a.id) c, p.capacidad cap from f_animales a left join f_potreros p on p.id=a.potrero_id where a.estado='Activo' group by p.nombre,p.capacidad order by 1"),
      san=q("select s.proxima_fecha f, s.tipo, s.producto, s.lote, a.arete from f_sanidad s left join f_animales a on a.id=s.animal_id where s.proxima_fecha<=%s order by 1 limit 12", (t + timedelta(days=15),)),
      low=q("select nombre, stock, minimo, unidad from f_insumos where stock<=minimo order by nombre"),
      partos=q("select r.fecha_parto_estimada f, a.arete from f_reproduccion r join f_animales a on a.id=r.hembra_id where r.fecha_parto_estimada between %s and %s order by 1", (t, t + timedelta(days=30))),
      trans=q("select * from f_traslados where estado<>'Entregado' order by fecha limit 10"),
      asis=q("select count(*) n, count(*) filter (where a.hora_llegada > p.hora_entrada) tarde from f_asistencia a join f_empleados e on e.id=a.empleado_id left join f_puestos p on p.id=e.puesto_id where a.fecha=%s and a.hora_llegada is not null", (t,), one=True))
    mods = [(k, e["t"]) for k, e in ENT.items() if e["r"] == "finca" or session["rol"] == "admin"]
    return render_template_string(T_HOME, d=d, mods=mods, hoy=t)

# ---------- personal: marcación y reporte ----------
def f_marcar():
    if "uid" not in session: abort(redirect("/login"))
    if session["rol"] not in ("personal", "admin"): abort(403)
    eid, now = request.form.get("empleado_id", ""), datetime.now(TZ)
    if not eid.isdigit(): flash("Seleccione un empleado"); return redirect("/f/asistencia")
    try:
        if request.form.get("accion") == "llegada":
            ex("insert into f_asistencia(fecha,empleado_id,hora_llegada,usuario) values(%s,%s,%s,%s)", (now.date(), int(eid), now.time().replace(microsecond=0), session["usuario"]))
            flash("Llegada registrada a las " + now.strftime("%H:%M"))
        elif ex("update f_asistencia set hora_salida=%s where fecha=%s and empleado_id=%s and hora_salida is null", (now.time().replace(microsecond=0), now.date(), int(eid))).rowcount:
            flash("Salida registrada a las " + now.strftime("%H:%M"))
        else: flash("No hay llegada abierta para hoy")
        db().commit()
    except (pgerr.UniqueViolation, pgerr.ForeignKeyViolation):
        db().rollback(); flash("Ya tiene llegada registrada hoy")
    return redirect("/f/asistencia")

def reporte_rows(mes):
    d1, d2 = rango(mes)
    return q("""select e.nombre, p.nombre puesto, p.hora_entrada, count(a.id) dias,
        coalesce(sum(case when a.hora_llegada > p.hora_entrada then 1 else 0 end),0) tardes,
        coalesce(round(sum(case when a.hora_llegada > p.hora_entrada then extract(epoch from (a.hora_llegada - p.hora_entrada))/60 else 0 end)::numeric),0) min_tarde,
        coalesce(round(sum(extract(epoch from (a.hora_salida - a.hora_llegada))/3600)::numeric,1),0) horas
        from f_empleados e left join f_puestos p on p.id=e.puesto_id
        left join f_asistencia a on a.empleado_id=e.id and a.fecha>=%s and a.fecha<%s
        where e.estado='Activo' group by e.id,e.nombre,p.nombre,p.hora_entrada order by e.nombre""", (d1, d2))

def f_reporte(fmt=""):
    if "uid" not in session: abort(redirect("/login"))
    if session["rol"] not in ("personal", "admin"): abort(403)
    mes = request.args.get("mes") or hoy().strftime("%Y-%m")
    rows = reporte_rows(mes)
    if fmt:
        return xlsx(f"asistencia_{mes}", ["Empleado", "Puesto", "Hora de entrada", "Días", "Tardanzas", "Minutos tarde", "Horas trabajadas"],
                    [[r["nombre"], r["puesto"], r["hora_entrada"], r["dias"], r["tardes"], r["min_tarde"], r["horas"]] for r in rows])
    return render_template_string(T_REP, rows=rows, mes=mes)

# ---------- datos de ejemplo para la demo ----------
def seed(c):
    def ins(k, **kw):
        return c.execute(f"insert into f_{k}({','.join(kw)}) values({','.join(['%s'] * len(kw))}) returning id", list(kw.values())).fetchone()["id"]
    t = date.today()
    pn = ins("potreros", nombre="Potrero Norte", hectareas=50, capacidad=60, pasto="Brachiaria")
    pr = ins("potreros", nombre="Potrero Río", hectareas=35, capacidad=40, pasto="Estrella")
    pc = ins("potreros", nombre="Corral de manejo", hectareas=2, capacidad=30)
    ids = []
    for i, (nom, raza, sx, cat, kg, pot, edad) in enumerate([("Canela", "Brahman", "Hembra", "Vaca", 430, pn, 1800), ("Tornado", "Brahman", "Macho", "Toro", 720, pn, 2500),
            ("Luna", "Carora", "Hembra", "Vaca", 410, pn, 1700), ("Manchas", "Cebú", "Hembra", "Novilla", 310, pr, 900), ("Rayo", "Cebú", "Macho", "Novillo", 360, pr, 800),
            ("Perla", "Brahman", "Hembra", "Mauta", 210, pr, 500), ("Chispa", "Cebú", "Macho", "Becerro", 130, pc, 200), ("Nube", "Carora", "Hembra", "Becerra", 120, pc, 190)], 1):
        ids.append(ins("animales", arete=f"A-{i:03d}", nombre=nom, especie="Bovino", raza=raza, sexo=sx, categoria=cat, peso_kg=kg, potrero_id=pot,
                       nacimiento=t - timedelta(days=edad), estado="Activo"))
    ins("sanidad", fecha=t - timedelta(days=170), tipo="Vacunación", producto="Fiebre aftosa", lote="Todo el rebaño", dosis="2 ml", responsable="Ana Rojas", proxima_fecha=t - timedelta(days=5))
    ins("sanidad", fecha=t - timedelta(days=20), tipo="Vacunación", producto="Brucelosis", animal_id=ids[5], dosis="2 ml", responsable="Ana Rojas", proxima_fecha=t + timedelta(days=10))
    ins("sanidad", fecha=t - timedelta(days=8), tipo="Desparasitación", producto="Ivermectina", lote="Potrero Río", dosis="1 ml/50 kg", responsable="Ana Rojas", proxima_fecha=t + timedelta(days=52))
    sal = ins("insumos", nombre="Sal mineral", tipo="Mineral/Sal", unidad="kg", stock=800, minimo=200, costo_unit="0.9")
    con = ins("insumos", nombre="Concentrado 18%", tipo="Alimento", unidad="kg", stock=1500, minimo=500, costo_unit="0.45")
    ins("insumos", nombre="Ivermectina", tipo="Medicina", unidad="frasco", stock=6, minimo=10, costo_unit="12")
    ins("entradas", fecha=t - timedelta(days=15), insumo_id=con, cantidad=2000, costo_total=900, proveedor="Agropecuaria Los Llanos")
    ins("consumos", fecha=t - timedelta(days=2), insumo_id=con, cantidad=500, potrero_id=pn, lote="Vacas en producción")
    ins("traslados", fecha=t - timedelta(days=6), tipo="Interno entre potreros", animal_id=ids[3], origen_potrero_id=pc, destino_potrero_id=pr, estado="Entregado")
    ins("traslados", fecha=t, tipo="Transporte externo", lote="Novillos de levante", cantidad=12, origen_potrero_id=pr, destino_texto="Frigorífico Los Llanos",
        vehiculo="A12BC3D", chofer="Pedro Díaz", guia="GM-000123", estado="En tránsito")
    ins("reproduccion", fecha=t - timedelta(days=70), tipo="Diagnóstico de preñez", hembra_id=ids[0], macho_id=ids[1], resultado="Positivo", fecha_parto_estimada=t + timedelta(days=20))
    cap = ins("puestos", nombre="Capataz", descripcion="Supervisa el personal y las labores", hora_entrada=dtime(6, 0), hora_salida=dtime(16, 0))
    vaq = ins("puestos", nombre="Vaquero", descripcion="Manejo diario del ganado", hora_entrada=dtime(6, 30), hora_salida=dtime(15, 30))
    vet = ins("puestos", nombre="Veterinario", descripcion="Sanidad y reproducción", hora_entrada=dtime(8, 0), hora_salida=dtime(16, 0))
    for nom, ced, pu, tel, lleg in [("Juan Pérez", "V-10111222", cap, "0414-0000001", dtime(5, 55)), ("Luis Gómez", "V-12333444", vaq, "0412-0000002", dtime(6, 50)),
                                   ("Ana Rojas", "V-14555666", vet, "0416-0000003", dtime(7, 58))]:
        e = ins("empleados", nombre=nom, cedula=ced, puesto_id=pu, telefono=tel, estado="Activo", fecha_ingreso=t - timedelta(days=400))
        for dd in (0, 1):
            ins("asistencia", fecha=t - timedelta(days=dd), empleado_id=e, hora_llegada=lleg, hora_salida=None if dd == 0 else dtime(16, 0))

# ---------- plantillas ----------
H = '{% extends "base" %}{% block c %}'; E = "{% endblock %}"
T_LIST = H + '''<h3>{{e.t}} <a href="/f/{{k}}/nuevo"><button>+ Nuevo</button></a> <a href="/f/{{k}}.xlsx"><button>Excel</button></a></h3>
{% if k=='asistencia' %}<form method=post action="/personal/marcar" class=box>{{T|safe}}<select name=empleado_id><option value="">Empleado…</option>{% for x in emp %}<option value={{x.id}}>{{x.nombre}}</option>{% endfor %}</select>
<button name=accion value=llegada>Marcar llegada ahora</button><button name=accion value=salida>Marcar salida ahora</button></form>{% endif %}
<form><input name=q value="{{q}}" placeholder="Buscar"><button>Buscar</button></form>
<table><tr>{% for c in e.cols %}<th>{{c.l}}{% endfor %}<th></tr>
{% for r in rows %}{% set al = e.alert and r[e.alert] and r[e.alert] < hoy %}{% set lo = e.low and r[e.low[0]] is not none and r[e.low[1]] is not none and r[e.low[0]] <= r[e.low[1]] %}<tr {% if al or lo %}class=mal{% endif %}>
{% for c in e.cols %}{% set v = r[c.n] %}<td {% if c.t=='num' %}class=r{% endif %}>{% if v is none %}{% elif c.t=='num' %}{{v|qty}}{% elif c.t=='time' %}{{v.strftime('%H:%M')}}{% else %}{{v}}{% endif %}{% endfor %}
<td><a href="/f/{{k}}/{{r.id}}/editar">Editar</a> · <a href="/f/{{k}}/{{r.id}}/borrar">Borrar</a></tr>{% endfor %}</table>''' + E
T_FORM = H + '''<h3>{{'Editar' if r else 'Nuevo'}} · {{e.t}}</h3><form method=post class="box ff">{{T|safe}}
{% for c in e.cols %}<label>{{c.l}}{% if c.req %} *{% endif %}
{% if c.t=='sel' %}<select name={{c.n}}><option value="">—</option>{% for o in c.o %}<option {% if sv[c.n]==o %}selected{% endif %}>{{o}}</option>{% endfor %}</select>
{% elif c.t=='fk' %}<select name={{c.n}}><option value="">—</option>{% for i,l in opts[c.n] %}<option value={{i}} {% if sv[c.n]==i %}selected{% endif %}>{{l}}</option>{% endfor %}</select>
{% else %}<input name={{c.n}} type="{{ {'date':'date','time':'time'}.get(c.t,'text') }}" {% if c.t in ('num','int') %}inputmode=decimal{% endif %} value="{{sv[c.n]}}">{% endif %}</label>{% endfor %}
<button>Guardar</button> <a href="/f/{{k}}">Cancelar</a></form>''' + E
T_DEL = H + '<div class=box><h3>¿Borrar este registro de {{e.t}}?</h3><form method=post>{{T|safe}}<button>Sí, borrar</button> <a href="/f/{{k}}">Cancelar</a></form></div>' + E
T_HOME = H + '''<h3>Resumen de la finca</h3>
<div class=cards style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px">
<div class=box><div style="color:#6b726e">Cabezas activas</div><div style="font-size:38px;font-weight:300">{{d.total}}</div>{% for r in d.cat %}<div>{{r.n}}: <b>{{r.c}}</b></div>{% endfor %}</div>
<div class=box><div style="color:#6b726e">Localización por potrero</div>{% for r in d.pot %}<div>{{r.n}}: <b>{{r.c}}</b>{% if r.cap %} / {{r.cap|qty}}{% endif %}</div>{% endfor %}</div>
<div class=box><div style="color:#6b726e">Asistencia de hoy</div><div style="font-size:38px;font-weight:300">{{d.asis.n}}</div><div>presentes · <span class=mal>{{d.asis.tarde}} tarde</span></div></div></div>
<h3>Sanidad: vencidas y próximas 15 días</h3><table><tr><th>Fecha<th>Tipo<th>Producto<th>Animal / lote</tr>{% for r in d.san %}<tr {% if r.f<hoy %}class=mal{% endif %}><td>{{r.f}}<td>{{r.tipo}}<td>{{r.producto}}<td>{{r.arete or r.lote or ''}}</tr>{% else %}<tr><td colspan=4>Sin pendientes{% endfor %}</table>
<h3>Insumos bajo el mínimo</h3><table><tr><th>Insumo<th class=r>Existencia<th class=r>Mínimo</tr>{% for r in d.low %}<tr class=mal><td>{{r.nombre}}<td class=r>{{r.stock|qty}} {{r.unidad}}<td class=r>{{r.minimo|qty}}</tr>{% else %}<tr><td colspan=3>Todo en orden{% endfor %}</table>
<h3>Partos estimados (30 días)</h3><table><tr><th>Fecha<th>Hembra</tr>{% for r in d.partos %}<tr><td>{{r.f}}<td>{{r.arete}}</tr>{% else %}<tr><td colspan=2>Ninguno{% endfor %}</table>
<h3>Traslados pendientes</h3><table><tr><th>Fecha<th>Tipo<th>Vehículo<th>Chofer<th>Guía<th>Estado</tr>{% for r in d.trans %}<tr><td>{{r.fecha}}<td>{{r.tipo}}<td>{{r.vehiculo or ''}}<td>{{r.chofer or ''}}<td>{{r.guia or ''}}<td>{{r.estado}}</tr>{% else %}<tr><td colspan=6>Ninguno{% endfor %}</table>
<h3>Módulos</h3><p>{% for k,t in mods %}<a href="/f/{{k}}"><button>{{t}}</button></a>{% endfor %}</p>''' + E
T_REP = H + '''<h3>Asistencia del personal · {{mes}}</h3><form>Mes <input type=month name=mes value={{mes}}><button>Ver</button> <a href="/personal/reporte.xlsx?mes={{mes}}"><button type=button>Excel</button></a></form>
<table><tr><th>Empleado<th>Puesto<th>Entrada<th class=r>Días<th class=r>Tardanzas<th class=r>Min. tarde<th class=r>Horas</tr>{% for r in rows %}<tr><td>{{r.nombre}}<td>{{r.puesto or ''}}<td>{{r.hora_entrada.strftime('%H:%M') if r.hora_entrada else ''}}<td class=r>{{r.dias}}<td class=r>{{r.tardes}}<td class=r>{{r.min_tarde}}<td class=r>{{r.horas}}</tr>{% endfor %}</table>''' + E

def register(app, h):
    globals().update(h)
    for rule, ep, fn, m in [("/f/<k>", "f_list", f_list, ["GET"]), ("/f/<k>.xlsx", "f_xlsx", f_xlsx, ["GET"]),
            ("/f/<k>/nuevo", "f_new", f_form, ["GET", "POST"]), ("/f/<k>/<int:id>/editar", "f_edit", f_form, ["GET", "POST"]),
            ("/f/<k>/<int:id>/borrar", "f_del", f_del, ["GET", "POST"]), ("/finca", "f_home", f_home, ["GET"]),
            ("/personal/marcar", "f_marcar", f_marcar, ["POST"]), ("/personal/reporte", "f_reporte", f_reporte, ["GET"]),
            ("/personal/reporte.xlsx", "f_reporte_x", lambda: f_reporte("x"), ["GET"])]:
        app.add_url_rule(rule, ep, fn, methods=m)
