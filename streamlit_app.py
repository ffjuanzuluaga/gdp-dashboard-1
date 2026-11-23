# -*- coding: utf-8 -*-
"""
Dashboard comercial de la línea de Transformación Digital, conectado a Odoo 19 vía
XML-RPC. El mismo Odoo aloja otras líneas de negocio de Firefly (p. ej. nómina/BPO),
así que el dashboard se restringe SIEMPRE al equipo de ventas "Transformación
Digital" (crm.team) para sale.order, account.move y crm.lead; el multiselect de
"equipos de venta" del sidebar solo sirve para desglosar entre sub-equipos DENTRO de
esa línea, nunca para incluir equipos de otras líneas. Los costos analíticos
(pestaña Rentabilidad) se identifican por nombre de cuenta analítica en Odoo, ya
que account.analytic.line no tiene equipo de ventas (ver LINEA_CUENTA_ANALITICA).
Pensado para desplegarse en Streamlit Community Cloud (share.streamlit.io).
"""

import re
import threading
import unicodedata
import xmlrpc.client
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

# ─────────────────────────────────────────────
# Configuración de página
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Dashboard Transformación Digital · Odoo 19",
    page_icon="📊",
    layout="wide",
)

# ─────────────────────────────────────────────
# Conexión a Odoo (XML-RPC)
# ─────────────────────────────────────────────
# get_connection() se cachea con cache_resource → el mismo ServerProxy (y su
# conexión HTTP persistente) se comparte entre TODAS las sesiones de usuario.
# xmlrpc.client no es thread-safe sobre una conexión compartida, así que hay que
# serializar las llamadas con un lock para evitar http.client.CannotSendRequest /
# ResponseNotReady cuando dos peticiones concurrentes reutilizan el mismo socket.
#
# OJO: el lock se crea DENTRO de get_connection() (y se cachea junto con la
# conexión) en vez de como variable global de módulo (`_odoo_lock = Lock()`
# suelto), porque Streamlit re-ejecuta el script en un namespace NUEVO en cada
# rerun (de cualquier sesión) — una variable global normal se recrearía en cada
# rerun y dejaría de ser el mismo objeto entre sesiones concurrentes, con lo que
# el lock no bloquearía nada de verdad. Solo lo que pasa por cache_resource /
# cache_data persiste de verdad entre reruns y sesiones.
@st.cache_resource(show_spinner="Conectando con Odoo...")
def get_connection():
    """Autentica una sola vez y reutiliza la conexión (y su lock) en toda la app."""
    cfg = st.secrets["odoo"]
    common = xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/common", allow_none=True)
    lock = threading.Lock()
    with lock:
        uid = common.authenticate(cfg["db"], cfg["username"], cfg["api_key"], {})
    if not uid:
        st.error("❌ Autenticación fallida. Verifica url, db, username y api_key en los Secrets.")
        st.stop()
    models = xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/object", allow_none=True)
    return cfg, uid, models, lock


def odoo_call(model: str, method: str, args: list, kwargs: dict | None = None):
    cfg, uid, models, lock = get_connection()
    with lock:
        return models.execute_kw(
            cfg["db"], uid, cfg["api_key"], model, method, args, kwargs or {}
        )


def search_read(model: str, domain: list, fields: list, **kw) -> pd.DataFrame:
    """search_read con paginación. Sin esto, un límite implícito del RPC puede
    truncar historiales grandes (p. ej. facturación TD) y dejar meses incompletos."""
    page_size = int(kw.pop("page_size", 5000))
    # Si el caller pide limit explícito, respetarlo (una sola página).
    if "limit" in kw:
        records = odoo_call(model, "search_read", [domain], {"fields": fields, **kw})
        df = pd.DataFrame(records)
        return df if not df.empty else pd.DataFrame(columns=fields)

    all_records: list = []
    offset = 0
    while True:
        batch = odoo_call(
            model, "search_read", [domain],
            {"fields": fields, "limit": page_size, "offset": offset, **kw},
        )
        if not batch:
            break
        all_records.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    df = pd.DataFrame(all_records)
    return df if not df.empty else pd.DataFrame(columns=fields)


def m2o_name(series: pd.Series) -> pd.Series:
    """Convierte columnas many2one [id, 'nombre'] → 'nombre'."""
    return series.apply(lambda v: v[1] if isinstance(v, (list, tuple)) and len(v) == 2 else "Sin asignar")


def m2o_id(series: pd.Series) -> pd.Series:
    return series.apply(_m2o_id_one)


def _m2o_id_one(v) -> int | None:
    if isinstance(v, (list, tuple)) and v:
        return int(v[0])
    if isinstance(v, int):
        return v
    return None


def _normalizar(s: str) -> str:
    """Mayúsculas y sin tildes, para comparar nombres de Odoo sin depender de
    cómo se hayan tildado/capitalizado (p. ej. 'Transformación Digital' vs.
    'TRANSFORMACION DIGITAL')."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").upper().strip()


# Nombre del equipo de ventas (crm.team) que delimita la línea de Transformación
# Digital dentro del Odoo compartido con otras líneas de negocio de Firefly.
NOMBRE_EQUIPO_TD = "TRANSFORMACION DIGITAL"


# ─────────────────────────────────────────────
# Carga de datos (cacheada 10 min)
# ─────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner="Cargando equipos de venta...")
def load_teams() -> pd.DataFrame:
    return search_read("crm.team", [], ["id", "name"], order="name")


@st.cache_data(ttl=600, show_spinner="Cargando roster de vendedores TD...")
def load_td_vendedores(team_ids: list[int]) -> pd.DataFrame:
    """Roster OFICIAL de vendedores de Transformación Digital: los usuarios
    miembros (Salespersons) de los equipos de venta de TD — crm.team.member_ids,
    confirmado en odoo/addons/sales_team/models/crm_team.py — no simplemente los
    nombres que aparecen en los datos ya filtrados por equipo. Se usa para
    excluir de cualquier tabla/gráfico gerencial a alguien que no sea vendedor de
    TD (p. ej. si registra horas en un proyecto de TD sin ser del equipo)."""
    equipos = search_read("crm.team", [("id", "in", team_ids)], ["id", "name", "member_ids"])
    if equipos.empty:
        return pd.DataFrame(columns=["user_id", "vendedor"])
    user_ids = sorted({uid for ids in equipos["member_ids"] for uid in (ids or [])})
    if not user_ids:
        return pd.DataFrame(columns=["user_id", "vendedor"])
    usuarios = search_read("res.users", [("id", "in", user_ids)], ["id", "name"])
    return filtrar_vendedores_excluidos(
        usuarios.rename(columns={"id": "user_id", "name": "vendedor"}))


@st.cache_data(ttl=600, show_spinner="Cargando órdenes de venta...")
def load_sales(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    domain = [
        ("date_order", ">=", date_from),
        ("date_order", "<=", f"{date_to} 23:59:59"),
        ("state", "in", ["sale", "done"]),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    df = search_read(
        "sale.order",
        domain,
        ["name", "date_order", "partner_id", "user_id", "team_id",
         "amount_untaxed", "state"],
        order="date_order",
    )
    if df.empty:
        return df
    df["date_order"] = pd.to_datetime(df["date_order"])
    df["equipo"] = m2o_name(df["team_id"])
    df["vendedor"] = m2o_name(df["user_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    return filtrar_vendedores_excluidos(df)


@st.cache_data(ttl=600, show_spinner="Cargando historial completo de facturación...")
def load_all_invoices(team_ids: list[int]) -> pd.DataFrame:
    """TODO el historial de facturas publicadas (sin límite de fecha), para poder
    saber la fecha de la primera factura de cada cliente y así distinguir clientes
    nuevos de recurrentes respecto al rango de fechas del sidebar."""
    domain = [
        ("move_type", "in", ["out_invoice", "out_refund"]),
        ("state", "=", "posted"),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    df = search_read(
        "account.move", domain,
        ["invoice_date", "partner_id", "amount_untaxed_signed"],
        order="invoice_date",
    )
    if df.empty:
        return df
    df["invoice_date"] = pd.to_datetime(df["invoice_date"])
    df["cliente_id"] = m2o_id(df["partner_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    return df


# Mapeo línea de negocio → categoría de producto en Odoo, usado para clasificar
# las líneas de factura (no existe un tag_ids en account.move como en crm.lead).
LINEA_CATEGORIA = {
    "Implementación Odoo": "Transformación digital",
    "Soporte y Mantenimiento": "Soporte",
    "Licenciamiento": "Licenciamiento",
}

# Mapeo línea de negocio → nombre de cuenta analítica en Odoo, usado para traer los
# costos del equipo (account.analytic.line no tiene equipo de ventas, así que la
# cuenta analítica es lo único que permite ubicar el costo dentro de la línea). El
# nombre de la cuenta no necesariamente coincide con el de la línea o la categoría
# de producto (p. ej. "Implementación Odoo" factura en la categoría "Transformación
# digital" pero sus costos están en la cuenta analítica "Implementación de software").
LINEA_CUENTA_ANALITICA = {
    "Implementación Odoo": "Implementación de software",
    "Soporte y Mantenimiento": "Soporte y Mantenimiento",
    "Licenciamiento": "Licenciamiento",
}
CUENTA_ANALITICA_LINEA = {v: k for k, v in LINEA_CUENTA_ANALITICA.items()}


def clasificar_tags_lineas(tag_ids: list[int] | None, tag_names: pd.Series) -> tuple[list[str], bool]:
    """A partir de los tag_ids (crm.tag) de una oportunidad, determina a qué
    línea(s) de LINEA_CATEGORIA pertenece y si es cliente NUEVO. Además de las
    etiquetas base ("Implementación Odoo", "Soporte y Mantenimiento"), en Odoo
    existe una variación con "Nuevo" (p. ej. "Nuevo Implementación Odoo") para
    marcar que la venta es de un cliente nuevo y no recurrente. Se compara por
    substring (sin tildes/mayúsculas) para que esa variante siga clasificando en
    su línea base en vez de caer en "Sin etiqueta"."""
    nombres_norm = [_normalizar(tag_names.get(i, "")) for i in (tag_ids or [])]
    nombres_norm = [n for n in nombres_norm if n]
    es_nuevo = any("NUEVO" in n for n in nombres_norm)
    lineas = [linea for linea in LINEA_CATEGORIA
              if any(_normalizar(linea) in n for n in nombres_norm)]
    return (lineas if lineas else ["Sin etiqueta"]), es_nuevo


MESES_ES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

# Proyecto de Odoo cuyo parte de horas registra la actividad comercial de TD.
# GC3/GC5/GC6/GC8 NO son el proyecto: son tareas DENTRO de "Gestión comercial".
PROYECTO_PARTE_HORAS_TD = "Gestion comercial"
PROYECTO_PARTE_HORAS_PATRONES = (
    "gestion comercial GC",
    "gestión comercial GC",
    "gestion comercial",
    "gestión comercial",
)

# Tipos de actividad comercial que pide el brief (cantidad en crm.activity.report
# y horas en partes GC3/GC5/GC6/GC8).
TIPOS_ACTIVIDAD = [
    "Presentación de negocio",
    "Propuesta",
    "Socialización / sustentación",
    "Seguimiento",
]
LINEAS_ACTIVIDAD = ["Implementación Odoo", "Soporte y Mantenimiento"]
# Iniciativas / oportunidades de CRM que no deben entrar en ningún KPI del dashboard.
CRM_LEAD_IDS_EXCLUIDOS = [1475]
# Vendedores a ocultar en TODO el dashboard (match por substring, sin tildes/mayúsculas).
VENDEDORES_EXCLUIDOS = ["MARITZA"]


def _excluir_crm_leads(domain: list) -> list:
    """Agrega al dominio el filtro de ids de crm.lead excluidos."""
    if CRM_LEAD_IDS_EXCLUIDOS:
        domain = list(domain) + [("id", "not in", CRM_LEAD_IDS_EXCLUIDOS)]
    return domain


def _vendedor_excluido(nombre) -> bool:
    n = _normalizar(str(nombre or ""))
    return any(ex in n for ex in VENDEDORES_EXCLUIDOS)


def filtrar_vendedores_excluidos(df: pd.DataFrame, col: str = "vendedor") -> pd.DataFrame:
    """Quita filas cuyo vendedor coincida con VENDEDORES_EXCLUIDOS."""
    if df is None or df.empty or col not in df.columns:
        return df
    return df[~df[col].map(_vendedor_excluido)].copy()


# Asuntos de correo (Re:/Fw:/…) que Odoo/emails a menudo crean como leads basura.
_RE_ASUNTO_CORREO = re.compile(r"^\s*(re|fw|fwd|rv|res)\s*:", re.IGNORECASE)


def es_lead_respuesta_correo(nombre) -> bool:
    """True si el nombre parece un asunto de respuesta/reenvío de correo."""
    return bool(_RE_ASUNTO_CORREO.match(str(nombre or "")))


def filtrar_leads_correo(df: pd.DataFrame, col: str = "name") -> pd.DataFrame:
    """Excluye leads/oportunidades creados desde respuestas de correo (Re:, Fw:, …)."""
    if df is None or df.empty or col not in df.columns:
        return df
    return df[~df[col].map(es_lead_respuesta_correo)].copy()


CODIGOS_HORAS_GC = {
    "GC3": "Presentación de negocio",
    "GC5": "Propuesta",
    "GC6": "Socialización / sustentación",
    "GC8": "Seguimiento",
}
CUBOS_CARTERA = [
    ("al_dia", "Al día"),
    ("d1_30", "1-30"),
    ("d31_60", "31-60"),
    ("d61_90", "61-90"),
    ("d91_120", "91-120"),
    ("d120_mas", "+120"),
]


def clasificar_tipo_actividad(nombre: str) -> str:
    """Mapea mail.activity.type al tipo del brief. Socialización/sustentación
    se evalúa antes que Propuesta para no tragarse 'sustentación de propuesta'."""
    n = _normalizar(nombre)
    if any(k in n for k in ("SOCIALIZ", "SUSTENT")):
        return "Socialización / sustentación"
    if "SEGUIMIENTO" in n:
        return "Seguimiento"
    if "PRESENTACION" in n or ("REUNION" in n and "NEGOCIO" in n):
        return "Presentación de negocio"
    if "PROPUESTA" in n or "COTIZACION" in n:
        return "Propuesta"
    return "Otras"


def clasificar_tarea_gc(nombre: str) -> str | None:
    """Clasifica una tarea/cuenta de parte de horas por código GC3/GC5/GC6/GC8."""
    n = _normalizar(nombre)
    for codigo, tipo in CODIGOS_HORAS_GC.items():
        if codigo in n:
            return tipo
    return None


def cubo_aging(fecha_venc, hoy: date, interval: int = 30) -> str:
    """Mismos cubos que account_reports.aged_receivable_report (intervalos de 30
    días, aging por fecha de vencimiento). period0 = Al día (vencimiento >= hoy)."""
    if pd.isna(fecha_venc):
        return "al_dia"
    venc = fecha_venc.date() if hasattr(fecha_venc, "date") else fecha_venc
    if venc >= hoy:
        return "al_dia"
    dias = (hoy - venc).days
    if dias <= interval:
        return "d1_30"
    if dias <= interval * 2:
        return "d31_60"
    if dias <= interval * 3:
        return "d61_90"
    if dias <= interval * 4:
        return "d91_120"
    return "d120_mas"


@st.cache_data(ttl=300, show_spinner="Cargando categorías de producto...")
def load_product_category_ids() -> dict[str, int]:
    """IDs de product.category por nombre (con match normalizado por tildes/caso),
    para filtrar account.invoice.report con child_of."""
    nombres = list(LINEA_CATEGORIA.values())
    cats = search_read("product.category", [("name", "in", nombres)], ["id", "name"])
    if cats.empty:
        # Fallback: traer categorías y mapear por nombre normalizado
        cats = search_read("product.category", [], ["id", "name"], order="id")
        if cats.empty:
            return {}
        wanted = {_normalizar(n): n for n in nombres}
        out: dict[str, int] = {}
        for _, row in cats.iterrows():
            key = _normalizar(str(row["name"] or ""))
            if key in wanted:
                out[wanted[key]] = int(row["id"])
        return out
    return {row["name"]: int(row["id"]) for _, row in cats.iterrows()}


def _moves_sin_reversión(move_ids: list[int]) -> set[int]:
    """account.move sin 'Movimiento de reversión' (filtro INFORME Odoo).
    Lectura por lotes: un solo search con id in [...] grandes puede truncarse."""
    ok: set[int] = set()
    if not move_ids:
        return ok
    for i in range(0, len(move_ids), 200):
        chunk = move_ids[i:i + 200]
        moves = search_read(
            "account.move", [("id", "in", chunk)],
            ["id", "reversal_move_ids"], limit=len(chunk),
        )
        if moves.empty:
            continue
        for _, row in moves.iterrows():
            if not (row.get("reversal_move_ids") or []):
                ok.add(int(row["id"]))
    return ok


def _dominio_informe_categoria(categ_id: int, year: int | None = None) -> list:
    """Dominio del filtro personalizado INFORME / Análisis de Facturas."""
    domain: list = [
        # (factura|NC cliente) ∧ (factura cliente|proveedor) ⇒ solo out_invoice
        ("move_type", "=", "out_invoice"),
        ("state", "not in", ["draft", "cancel"]),
        ("product_categ_id", "=", categ_id),
        ("move_id.reversal_move_ids", "=", False),
    ]
    if year is not None:
        domain += [
            ("invoice_date", ">=", f"{year}-01-01"),
            ("invoice_date", "<=", f"{year}-12-31"),
        ]
    return domain


def _mes_key_from_group(label) -> str | None:
    """Normaliza etiqueta de read_group invoice_date:month → YYYY-MM."""
    if label is None:
        return None
    if isinstance(label, (list, tuple)) and label:
        label = label[0]
    s = str(label)
    if len(s) >= 7 and s[4] == "-":
        return s[:7]
    # "enero 2026" / "January 2026"
    meses = {
        "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
        "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
        "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
    }
    parts = s.strip().lower().replace(",", " ").split()
    if len(parts) >= 2:
        m = meses.get(parts[0])
        y = parts[-1]
        if m and y.isdigit() and len(y) == 4:
            return f"{y}-{m}"
    return None


@st.cache_data(ttl=120, show_spinner="Cargando facturación por categoría...")
def load_invoice_lines_by_categoria(categoria: str, team_ids: list[int] | None = None) -> pd.DataFrame:
    """Líneas facturadas de una categoría, COP sin IVA (dominio INFORME)."""
    del team_ids
    categ_ids = load_product_category_ids()
    categ_id = categ_ids.get(categoria)
    if not categ_id:
        return pd.DataFrame(columns=["date", "partner_id", "monto", "mes",
                                     "cliente_id", "cliente", "vendedor"])
    domain = _dominio_informe_categoria(categ_id)
    fields_air = ["invoice_date", "partner_id", "invoice_user_id", "team_id",
                  "price_subtotal", "move_id", "product_categ_id"]
    df = pd.DataFrame()
    try:
        try:
            df = search_read(
                "account.invoice.report", domain, fields_air,
                order="invoice_date asc, id asc",
            )
        except Exception:
            # Si el dotted reversal falla en el report, cargamos sin él y filtramos.
            domain_no_rev = [d for d in domain if not (
                isinstance(d, (list, tuple)) and d and "reversal" in str(d[0]))]
            df = search_read("account.invoice.report", domain_no_rev, fields_air)
            if not df.empty:
                move_ids = sorted({mid for mid in (_m2o_id_one(v) for v in df["move_id"].tolist()) if mid})
                ok_ids = _moves_sin_reversión(move_ids)
                df = df[df["move_id"].map(lambda m: _m2o_id_one(m) in ok_ids)].copy()
    except Exception:
        df = pd.DataFrame()
    if df.empty:
        domain_aml = [
            ("move_id.move_type", "=", "out_invoice"),
            ("move_id.state", "not in", ["draft", "cancel"]),
            ("display_type", "=", "product"),
            ("product_id.categ_id", "=", categ_id),
        ]
        df = search_read(
            "account.move.line", domain_aml,
            ["date", "partner_id", "balance", "move_id"],
            order="date asc, id asc",
        )
        if df.empty:
            return df
        move_ids = sorted({mid for mid in (_m2o_id_one(v) for v in df["move_id"].tolist()) if mid})
        ok_ids = _moves_sin_reversión(move_ids)
        df = df[df["move_id"].map(lambda m: _m2o_id_one(m) in ok_ids)].copy()
        if df.empty:
            return df
        move_dates: dict[int, object] = {}
        for i in range(0, len(move_ids), 200):
            chunk = [mid for mid in move_ids[i:i + 200] if mid in ok_ids]
            if not chunk:
                continue
            moves = search_read("account.move", [("id", "in", chunk)],
                                ["id", "invoice_date"], limit=len(chunk))
            for _, r in moves.iterrows():
                move_dates[int(r["id"])] = r.get("invoice_date")
        df["invoice_date"] = df["move_id"].apply(lambda m: move_dates.get(_m2o_id_one(m)))
        df["invoice_date"] = df["invoice_date"].fillna(df["date"])
        df["invoice_user_id"] = None
        df["price_subtotal"] = -pd.to_numeric(df["balance"], errors="coerce").fillna(0)
    if df.empty:
        return df
    # Reaplicar filtro de reversión por lotes (garantiza paridad aunque el
    # dominio dotted en account.invoice.report no esté disponible).
    if "move_id" in df.columns:
        move_ids = sorted({mid for mid in (_m2o_id_one(v) for v in df["move_id"].tolist()) if mid})
        ok_ids = _moves_sin_reversión(move_ids)
        df = df[df["move_id"].map(lambda m: _m2o_id_one(m) in ok_ids)].copy()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["invoice_date"], errors="coerce").dt.normalize()
    df["monto"] = pd.to_numeric(df["price_subtotal"], errors="coerce").fillna(0)
    df["mes"] = df["date"].dt.strftime("%Y-%m")
    df["cliente_id"] = m2o_id(df["partner_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    df["vendedor"] = m2o_name(df["invoice_user_id"])
    return df


@st.cache_data(ttl=120, show_spinner="Cargando pivote de facturación por línea...")
def load_facturacion_pivote_anual(year: int) -> pd.DataFrame:
    """Pivote mes × categoría vía read_group (misma agregación que el pivote Odoo)."""
    meses = [f"{year}-{m:02d}" for m in range(1, 13)]
    orden_cats = ["Licenciamiento", "Soporte", "Transformación digital"]
    categ_ids = load_product_category_ids()
    filas = []
    for categoria in orden_cats:
        categ_id = categ_ids.get(categoria)
        if not categ_id:
            continue
        domain = _dominio_informe_categoria(categ_id, year=year)
        groups = None
        try:
            groups = odoo_call(
                "account.invoice.report", "read_group",
                [domain, ["price_subtotal"], ["invoice_date:month"]],
                {"lazy": False},
            )
        except Exception:
            groups = None
        if groups is not None:
            for g in groups:
                mes = _mes_key_from_group(
                    g.get("invoice_date:month") or g.get("invoice_date"))
                if not mes:
                    continue
                filas.append({
                    "mes": mes,
                    "categoria": categoria,
                    "monto": float(g.get("price_subtotal") or 0),
                })
            continue
        # Fallback: líneas
        lf = load_invoice_lines_by_categoria(categoria)
        if lf.empty:
            continue
        lf = lf[lf["date"].dt.year == year]
        for mes, monto in lf.groupby("mes")["monto"].sum().items():
            filas.append({"mes": mes, "categoria": categoria, "monto": float(monto)})
    if not filas:
        piv = pd.DataFrame(0.0, index=meses, columns=orden_cats)
    else:
        bruto = pd.DataFrame(filas)
        piv = (bruto.pivot_table(index="mes", columns="categoria", values="monto",
                                 aggfunc="sum", fill_value=0.0)
               .reindex(index=meses, fill_value=0.0))
        for c in orden_cats:
            if c not in piv.columns:
                piv[c] = 0.0
        piv = piv[orden_cats]
    piv["Total"] = piv[orden_cats].sum(axis=1)
    total_row = piv.sum(axis=0).to_frame().T
    total_row.index = ["Total"]
    piv = pd.concat([total_row, piv])
    piv.index.name = "mes"
    return piv.reset_index().rename(columns={"mes": "Mes"})


@st.cache_data(ttl=600, show_spinner="Cargando informe de facturas (COP, sin IVA)...")
def load_invoice_report(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Líneas de account.invoice.report del período, en COP sin impuestos.
    Sirve para tops de vendedores/clientes por facturado."""
    domain = [
        ("move_type", "in", ["out_invoice", "out_refund"]),
        ("state", "=", "posted"),
        ("invoice_date", ">=", date_from),
        ("invoice_date", "<=", date_to),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    try:
        df = search_read(
            "account.invoice.report", domain,
            ["invoice_date", "partner_id", "invoice_user_id", "team_id", "price_subtotal"],
            order="invoice_date",
        )
    except Exception:
        domain_aml = [
            ("move_id.move_type", "in", ["out_invoice", "out_refund"]),
            ("move_id.state", "=", "posted"),
            ("display_type", "=", "product"),
            ("date", ">=", date_from),
            ("date", "<=", date_to),
        ]
        if team_ids:
            domain_aml.append(("move_id.team_id", "in", team_ids))
        df = search_read(
            "account.move.line", domain_aml,
            ["date", "partner_id", "balance"],
            order="date",
        )
        if df.empty:
            return df
        df["invoice_date"] = df["date"]
        df["invoice_user_id"] = None
        df["team_id"] = None
        df["price_subtotal"] = -pd.to_numeric(df["balance"], errors="coerce").fillna(0)
    if df.empty:
        return df
    df["invoice_date"] = pd.to_datetime(df["invoice_date"])
    df["monto"] = pd.to_numeric(df["price_subtotal"], errors="coerce").fillna(0)
    df["vendedor"] = m2o_name(df["invoice_user_id"]) if "invoice_user_id" in df.columns else "Sin asignar"
    df["cliente"] = m2o_name(df["partner_id"])
    df["equipo"] = m2o_name(df["team_id"]) if "team_id" in df.columns else "Sin asignar"
    return filtrar_vendedores_excluidos(df)


@st.cache_data(ttl=600, show_spinner="Cargando facturas...")
def load_invoices(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    domain = [
        ("move_type", "in", ["out_invoice", "out_refund"]),
        ("state", "=", "posted"),
        ("invoice_date", ">=", date_from),
        ("invoice_date", "<=", date_to),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    df = search_read(
        "account.move",
        domain,
        ["name", "invoice_date", "invoice_date_due", "partner_id", "invoice_user_id",
         "team_id", "amount_untaxed_signed",
         "amount_residual_signed", "payment_state", "move_type"],
        order="invoice_date",
    )
    if df.empty:
        return df
    df["invoice_date"] = pd.to_datetime(df["invoice_date"])
    df["invoice_date_due"] = pd.to_datetime(df["invoice_date_due"])
    df["equipo"] = m2o_name(df["team_id"])
    df["vendedor"] = m2o_name(df["invoice_user_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    return filtrar_vendedores_excluidos(df)


@st.cache_data(ttl=600, show_spinner="Cargando cartera (Aged Receivable)...")
def load_aged_receivable(team_ids: list[int], hoy_iso: str):
    """Cartera vigente con la misma lógica que Contabilidad > Aged Receivable
    (account_reports.aged_receivable_report): residual de líneas receivable
    publicadas, aging por date_maturity, cubos de 30 días. Se filtra además por
    equipo TD (el informe oficial es de toda la compañía)."""
    cols = ["date", "invoice_date", "date_maturity", "partner_id", "move_id",
            "amount_residual"]
    domain = [
        ("account_id.account_type", "=", "asset_receivable"),
        ("parent_state", "=", "posted"),
        ("amount_residual", "!=", 0),
    ]
    if team_ids:
        domain.append(("move_id.team_id", "in", team_ids))
    try:
        df = search_read("account.move.line", domain, cols)
    except Exception as e:
        return pd.DataFrame(columns=cols), (
            f"No se pudo consultar cartera (líneas receivable, misma lógica que "
            f"`account_reports.aged_receivable_report`): {e}"
        )
    if df.empty:
        return df, None
    hoy_d = date.fromisoformat(hoy_iso)
    df["invoice_date"] = pd.to_datetime(df["invoice_date"], errors="coerce")
    df["date_maturity"] = pd.to_datetime(df["date_maturity"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["vencimiento"] = df["date_maturity"].fillna(df["date"])
    df["saldo"] = pd.to_numeric(df["amount_residual"], errors="coerce").fillna(0)
    df["cubo"] = df["vencimiento"].apply(lambda v: cubo_aging(v, hoy_d))
    df["cliente"] = m2o_name(df["partner_id"])
    df["factura"] = m2o_name(df["move_id"])
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando pipeline CRM...")
def load_leads(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Leads + oportunidades del período (ambos viven en crm.lead).
    Antes solo se pedían oportunidades (`type=opportunity`), y los leads aún no
    convertidos (p. ej. sin vendedor) no aparecían en el pipeline del dashboard."""
    # active in (True, False) para incluir perdidos/archivados
    domain = [
        ("create_date", ">=", date_from),
        ("create_date", "<=", f"{date_to} 23:59:59"),
        ("type", "in", ["lead", "opportunity"]),
        "|", ("active", "=", True), ("active", "=", False),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    domain = _excluir_crm_leads(domain)
    df = search_read(
        "crm.lead",
        domain,
        ["name", "create_date", "date_closed", "partner_id", "user_id", "team_id",
         "stage_id", "expected_revenue", "probability", "active", "won_status", "type"],
        order="create_date",
    )
    if df.empty:
        return df
    df["create_date"] = pd.to_datetime(df["create_date"])
    df["equipo"] = m2o_name(df["team_id"])
    df["vendedor"] = m2o_name(df["user_id"])
    df["etapa"] = m2o_name(df["stage_id"])
    df["tipo"] = df["type"].map({"lead": "Lead", "opportunity": "Oportunidad"}).fillna(df["type"])
    df["estado"] = df["won_status"].map({"won": "Ganada", "lost": "Perdida", "pending": "Abierta"})
    return filtrar_leads_correo(filtrar_vendedores_excluidos(df))


@st.cache_data(ttl=600, show_spinner="Cargando metas...")
def load_metas() -> pd.DataFrame:
    """Metas anuales por vendedor desde metas.csv (en la raíz del repo).
    El nombre en la columna 'vendedor' debe coincidir EXACTAMENTE con el
    nombre del usuario en Odoo."""
    try:
        df = pd.read_csv("data/metas.csv")
        df["vendedor"] = df["vendedor"].str.strip()
        return filtrar_vendedores_excluidos(df)
    except FileNotFoundError:
        return pd.DataFrame(columns=["vendedor", "meta_anual"])


@st.cache_data(ttl=600, show_spinner="Cargando metas por línea...")
def load_metas_lineas() -> pd.DataFrame:
    """Metas anuales por línea de negocio desde data/metas_lineas.csv.
    El nombre en la columna 'linea' debe coincidir EXACTAMENTE con el
    nombre de la etiqueta (crm.tag) en Odoo."""
    try:
        df = pd.read_csv("data/metas_lineas.csv")
        df["linea"] = df["linea"].str.strip()
        return df
    except FileNotFoundError:
        return pd.DataFrame(columns=["linea", "meta_anual"])


@st.cache_data(ttl=600, show_spinner="Cargando etiquetas CRM...")
def load_tags() -> pd.DataFrame:
    return search_read("crm.tag", [], ["id", "name"], order="name")


@st.cache_data(ttl=600, show_spinner="Cargando oportunidades ganadas...")
def load_won(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Oportunidades GANADAS con fecha de cierre en el período (reporte de ganados).
    GANADA = won_status == 'won' (probability 100 Y stage_id.is_won — el campo real
    de Odoo, más estricto que solo probability == 100)."""
    domain = [
        ("type", "=", "opportunity"),
        ("won_status", "=", "won"),
        ("date_closed", ">=", date_from),
        ("date_closed", "<=", f"{date_to} 23:59:59"),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    domain = _excluir_crm_leads(domain)
    df = search_read(
        "crm.lead",
        domain,
        ["name", "date_closed", "user_id", "team_id", "partner_id", "expected_revenue"],
        order="date_closed",
    )
    if df.empty:
        return df
    df["date_closed"] = pd.to_datetime(df["date_closed"])
    df["mes_num"] = df["date_closed"].dt.month
    df["mes"] = df["date_closed"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    return filtrar_leads_correo(filtrar_vendedores_excluidos(df))


@st.cache_data(ttl=600, show_spinner="Cargando ganados por línea...")
def load_won_lineas(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Oportunidades GANADAS con fecha de cierre en el período, explotadas por etiqueta
    (línea de negocio). Si una oportunidad tiene varias etiquetas, su valor se cuenta
    en cada una. También expone si la etiqueta marca cliente nuevo (ver
    clasificar_tags_lineas). GANADA = won_status == 'won' (ver load_won)."""
    domain = [
        ("type", "=", "opportunity"),
        ("won_status", "=", "won"),
        ("date_closed", ">=", date_from),
        ("date_closed", "<=", f"{date_to} 23:59:59"),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    domain = _excluir_crm_leads(domain)
    cols = ["name", "date_closed", "vendedor", "equipo", "expected_revenue", "mes",
            "linea", "es_nuevo", "segmento"]
    df = search_read(
        "crm.lead", domain,
        ["name", "date_closed", "user_id", "team_id", "expected_revenue", "tag_ids"],
        order="date_closed",
    )
    if df.empty:
        return pd.DataFrame(columns=cols)
    tag_names = load_tags().set_index("id")["name"]
    df["date_closed"] = pd.to_datetime(df["date_closed"])
    df["mes"] = df["date_closed"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    clasificacion = df["tag_ids"].apply(lambda ids: clasificar_tags_lineas(ids, tag_names))
    df["linea"] = clasificacion.apply(lambda c: c[0])
    df["es_nuevo"] = clasificacion.apply(lambda c: c[1])
    df["segmento"] = df["es_nuevo"].map({True: "Nuevo", False: "Recurrente"})
    df = df.explode("linea")
    return filtrar_leads_correo(filtrar_vendedores_excluidos(df[cols]))


@st.cache_data(ttl=600, show_spinner="Cargando cumplimiento mensual por línea...")
def load_cumplimiento_mensual(year: int, team_ids: list[int]) -> pd.DataFrame:
    """Ventas / facturado por línea y mes para el año, SIN IVA.
    Fuente única: account.invoice.report (misma que Análisis de Facturas).
    'vendido' y 'facturado' son el mismo monto (facturado antes de impuestos);
    se mantienen ambas columnas por compatibilidad con pivotes/gráficos.
    `team_ids` no aplica: la clasificación es por categoría de producto."""
    del team_ids
    partes = []
    for linea, categoria in LINEA_CATEGORIA.items():
        lineas_fact = load_invoice_lines_by_categoria(categoria)
        if lineas_fact.empty:
            continue
        lineas_fact = lineas_fact[lineas_fact["date"].dt.year == year]
        if lineas_fact.empty:
            continue
        resumen = (lineas_fact.groupby("mes", as_index=False)["monto"]
                  .sum().rename(columns={"monto": "facturado"}))
        resumen["linea"] = linea
        partes.append(resumen)
    facturado = (pd.concat(partes, ignore_index=True) if partes
                else pd.DataFrame(columns=["mes", "facturado", "linea"]))

    meses = [f"{year}-{m:02d}" for m in range(1, 13)]
    base = pd.MultiIndex.from_product(
        [list(LINEA_CATEGORIA.keys()), meses], names=["linea", "mes"]).to_frame(index=False)
    tabla = base.merge(facturado, on=["linea", "mes"], how="left")
    tabla["facturado"] = tabla["facturado"].fillna(0)
    # Ventas del cumplimiento = facturado antes de impuestos (no CRM).
    tabla["vendido"] = tabla["facturado"]
    return tabla


def meta_prorateada_a_hoy(meta_anual: float, year: int, hoy: date) -> float:
    """Meta prorateada según el avance del año, para poder comparar 'vamos en línea
    con la meta' además del cumplimiento sobre la meta anual completa. Ejemplo: meta
    anual 12M, hoy es 30 de junio (mitad del año) → meta prorateada 6M; si llevo
    vendidos 6M, el cumplimiento A LA FECHA es 100% (voy como se esperaba a estas
    alturas del año), aunque el cumplimiento ANUAL sea apenas 50%. Para años ya
    cerrados devuelve la meta completa (el año entero ya transcurrió); para años
    futuros devuelve 0."""
    if year < hoy.year:
        return meta_anual
    if year > hoy.year:
        return 0.0
    dias_transcurridos = (hoy - date(year, 1, 1)).days + 1
    dias_anio = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    return meta_anual * dias_transcurridos / dias_anio


def construir_pivote_cumplimiento(datos: pd.DataFrame, value_col: str, year: int,
                                   metas_lineas: pd.DataFrame, hoy: date) -> pd.DataFrame:
    """Matriz línea (filas, + TOTAL) x mes (columnas, + Acumulado + % Cumpl.
    Anual + % Cumpl. a la Fecha), al estilo del reporte de referencia en
    Excel/Sheets. % Cumpl. a la Fecha solo se calcula para columnas de valor
    acumulable (vendido/facturado) — no aplica para "diferencia"."""
    incluir_a_la_fecha = value_col != "diferencia"
    filas = []
    for linea in LINEA_CATEGORIA:
        meta_row = metas_lineas.loc[metas_lineas["linea"] == linea, "meta_anual"]
        meta_anual = float(meta_row.iloc[0]) if not meta_row.empty else 0.0
        fila = {"Línea": linea}
        acumulado = 0.0
        for m, nombre_mes in enumerate(MESES_ES, start=1):
            mes_key = f"{year}-{m:02d}"
            valor = datos.loc[
                (datos["linea"] == linea) & (datos["mes"] == mes_key), value_col].sum()
            fila[nombre_mes] = valor
            acumulado += valor
        fila["Acumulado"] = acumulado
        fila["% Cumpl. Anual"] = (acumulado / meta_anual * 100) if meta_anual else 0.0
        if incluir_a_la_fecha:
            meta_a_la_fecha = meta_prorateada_a_hoy(meta_anual, year, hoy)
            fila["% Cumpl. a la Fecha"] = (acumulado / meta_a_la_fecha * 100) if meta_a_la_fecha else 0.0
            fila["_meta_a_la_fecha"] = meta_a_la_fecha
        fila["_meta_anual"] = meta_anual
        filas.append(fila)

    tabla = pd.DataFrame(filas)
    total = {"Línea": "TOTAL"}
    for nombre_mes in MESES_ES:
        total[nombre_mes] = tabla[nombre_mes].sum()
    total["Acumulado"] = tabla["Acumulado"].sum()
    meta_total = tabla["_meta_anual"].sum()
    total["% Cumpl. Anual"] = (total["Acumulado"] / meta_total * 100) if meta_total else 0.0
    if incluir_a_la_fecha:
        meta_a_la_fecha_total = tabla["_meta_a_la_fecha"].sum()
        total["% Cumpl. a la Fecha"] = (
            total["Acumulado"] / meta_a_la_fecha_total * 100) if meta_a_la_fecha_total else 0.0
        tabla = tabla.drop(columns="_meta_a_la_fecha")
    tabla = tabla.drop(columns="_meta_anual")
    return pd.concat([tabla, pd.DataFrame([total])], ignore_index=True)


@st.cache_data(ttl=600, show_spinner="Cargando leads y pipeline completo...")
def load_leads_full(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Todo el pipeline (leads + oportunidades) con medio (utm.medium), para el
    reporte de conversión."""
    domain = [
        ("create_date", ">=", date_from),
        ("create_date", "<=", f"{date_to} 23:59:59"),
        "|", ("active", "=", True), ("active", "=", False),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    domain = _excluir_crm_leads(domain)
    df = search_read(
        "crm.lead", domain,
        ["name", "create_date", "user_id", "team_id", "medium_id", "source_id",
         "expected_revenue", "probability", "active", "type", "won_status"],
        order="create_date",
    )
    if df.empty:
        return df
    df["create_date"] = pd.to_datetime(df["create_date"])
    df["mes"] = df["create_date"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    df["medio"] = m2o_name(df["medium_id"])
    df["origen"] = m2o_name(df["source_id"])
    df["estado"] = df["won_status"].map({"won": "Ganada", "lost": "Perdida", "pending": "Abierta"})
    return filtrar_leads_correo(filtrar_vendedores_excluidos(df))


@st.cache_data(ttl=600, show_spinner="Cargando cierres del período...")
def load_pipeline_closed(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Oportunidades GANADAS o PERDIDAS (won_status en 'won'/'lost') con fecha de
    cierre en el período, sin importar cuándo se crearon (incluye leads heredados
    de períodos anteriores que se cerraron ahora). Se usa para la tasa de
    conversión. date_closed puede quedar registrado también en casos que Odoo NO
    considera won_status 'won' ni 'lost' (p. ej. archivado a media probabilidad),
    así que se filtra explícitamente por won_status en vez de solo por que
    date_closed exista."""
    domain = [
        ("type", "=", "opportunity"),
        ("won_status", "in", ["won", "lost"]),
        ("date_closed", ">=", date_from),
        ("date_closed", "<=", f"{date_to} 23:59:59"),
        "|", ("active", "=", True), ("active", "=", False),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    domain = _excluir_crm_leads(domain)
    df = search_read(
        "crm.lead", domain,
        ["name", "create_date", "date_closed", "user_id", "team_id", "medium_id",
         "expected_revenue", "probability", "active", "won_status"],
        order="date_closed",
    )
    if df.empty:
        return df
    df["create_date"] = pd.to_datetime(df["create_date"])
    df["date_closed"] = pd.to_datetime(df["date_closed"])
    df["mes"] = df["date_closed"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    df["medio"] = m2o_name(df["medium_id"])
    df["estado"] = df["won_status"].map({"won": "Ganada", "lost": "Perdida"})
    # Días entre creación y cierre (ciclo comercial).
    df["dias_ciclo"] = (df["date_closed"] - df["create_date"]).dt.total_seconds() / 86400.0
    df["dias_ciclo"] = df["dias_ciclo"].clip(lower=0).round(1)
    return filtrar_leads_correo(filtrar_vendedores_excluidos(df))


@st.cache_data(ttl=600, show_spinner="Cargando meta de contratos de soporte...")
def load_meta_contratos() -> float:
    """Meta anual de contratos de soporte activos (en número), desde
    data/metas_contratos.csv (una sola columna, una sola fila)."""
    try:
        df = pd.read_csv("data/metas_contratos.csv")
        return float(df["meta_contratos_anual"].iloc[0])
    except (FileNotFoundError, IndexError, KeyError):
        return 0.0


@st.cache_data(ttl=600, show_spinner="Cargando contratos de soporte activos...")
def load_active_support_contracts():
    """Contratos de soporte activos (res.partner del módulo support_contract).
    Devuelve (estandar, estabilizacion, error). Si el módulo no está instalado
    o cambiaron los campos, error trae el mensaje y los DataFrames van vacíos,
    para no tumbar el resto del dashboard."""
    cols_std = ["name", "support_contract_ref", "support_contract_state",
                "support_contract_start", "support_contract_end",
                "support_ticket_limit", "support_hours_limit"]
    cols_estab = ["name", "estabilizacion_contract_state",
                  "estabilizacion_contract_start", "estabilizacion_contract_end"]
    try:
        estandar = search_read(
            "res.partner",
            [("has_support_contract", "=", True),
             ("support_contract_state", "in", ["active", "expiring_soon"])],
            cols_std,
        )
        estabilizacion = search_read(
            "res.partner",
            [("has_estabilizacion_contract", "=", True),
             ("estabilizacion_contract_state", "in", ["active", "expiring_soon"])],
            cols_estab,
        )
        return estandar, estabilizacion, None
    except Exception as e:
        msg = (f"No se pudo consultar contratos de soporte (módulo `support_contract` "
               f"sobre res.partner): {e}")
        return pd.DataFrame(columns=cols_std), pd.DataFrame(columns=cols_estab), msg


@st.cache_data(ttl=600, show_spinner="Cargando horas vendidas...")
def load_sold_hours_lines(date_from: str, date_to: str, team_ids: list[int]):
    """Líneas de sale.order.line con horas vendidas, de órdenes confirmadas. Dos
    fuentes posibles de horas en este Odoo (módulo propio `custom_add_hours`):
    (1) el campo `horas` (custom, en product_template_attribute_value_ids.horas
    según Complejidad/Tamaño — usado en productos de proyecto tipo Implementación,
    donde product_uom_qty queda igualado a esas horas pero la UoM del producto NO
    necesariamente se llama 'Horas'); (2) líneas cuya UoM sí se llama 'Horas' de
    forma literal (venta simple por horas). Se usa `horas` cuando está poblado y
    si no se cae a `product_uom_qty`. Se cruza luego con `sales` (ya cargado para
    el mismo período) para saber mes/vendedor/equipo."""
    domain = [
        ("order_id.date_order", ">=", date_from),
        ("order_id.date_order", "<=", f"{date_to} 23:59:59"),
        ("order_id.state", "in", ["sale", "done"]),
        "|", ("product_uom_id.name", "ilike", "hora"), ("horas", ">", 0),
    ]
    if team_ids:
        domain.append(("order_id.team_id", "in", team_ids))
    cols = ["order_id", "product_uom_qty", "product_uom_id", "horas"]
    try:
        df = search_read("sale.order.line", domain, cols)
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo consultar horas vendidas (sale.order.line): {e}"
    if df.empty:
        return df, None
    df["order_id_num"] = m2o_id(df["order_id"])
    df["horas_vendidas"] = df.apply(
        lambda r: r["horas"] if r["horas"] else r["product_uom_qty"], axis=1)
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando reporte de actividades CRM...")
def load_crm_activity_report(date_from: str, date_to: str, team_ids: list[int]):
    """Actividades del equipo desde crm.activity.report — el reporte estándar de
    Odoo detrás de 'CRM > Informes > Actividades'. Es una vista SQL sobre
    mail.message (no sobre mail.activity), por eso SÍ conserva el histórico
    completo de actividades ya completadas y permite una tendencia mensual real.
    'date' = fecha en que se completó la actividad; 'user_id' = vendedor dueño de
    la oportunidad (l.user_id), no necesariamente quien registró la actividad;
    'tag_ids' es un related a lead_id.tag_ids (confirmado en
    odoo/addons/crm/report/crm_activity_report.py). Se clasifica por línea con las
    mismas etiquetas de crm.lead (ver clasificar_tags_lineas) y solo se dejan
    Implementación Odoo y Soporte y Mantenimiento — Licenciamiento no tiene este
    seguimiento de actividad comercial."""
    cols = ["date", "mail_activity_type_id", "user_id", "team_id", "tag_ids"]
    domain = [
        ("date", ">=", date_from),
        ("date", "<=", f"{date_to} 23:59:59"),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    if CRM_LEAD_IDS_EXCLUIDOS:
        domain.append(("lead_id", "not in", CRM_LEAD_IDS_EXCLUIDOS))
    try:
        df = search_read("crm.activity.report", domain, cols, order="date")
    except Exception as e:
        return pd.DataFrame(columns=cols), (
            f"No se pudo consultar crm.activity.report: {e}. Se intentó con los campos "
            f"`date`, `mail_activity_type_id`, `user_id`, `team_id`, `tag_ids`."
        )
    if df.empty:
        return df, None
    tag_names = load_tags().set_index("id")["name"]
    df["date"] = pd.to_datetime(df["date"])
    df["mes"] = df["date"].dt.to_period("M").astype(str)
    df["tipo"] = m2o_name(df["mail_activity_type_id"])
    df["tipo_brief"] = df["tipo"].apply(clasificar_tipo_actividad)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    clasificacion = df["tag_ids"].apply(lambda ids: clasificar_tags_lineas(ids, tag_names))
    df["linea"] = clasificacion.apply(lambda c: c[0])
    df = df.explode("linea")
    df = df[df["linea"].isin(LINEAS_ACTIVIDAD)]
    return filtrar_vendedores_excluidos(df), None


@st.cache_data(ttl=600, show_spinner="Buscando proyecto Gestión comercial...")
def load_proyecto_gestion_comercial() -> tuple[pd.DataFrame, str | None]:
    """Localiza el proyecto de parte de horas comercial. Primero por nombre
    (Gestión comercial); si no aparece, por las tareas GC3/GC5/GC6/GC8 y el
    project_id de esas tareas."""
    vistos: set[int] = set()
    partes = []
    for patron in PROYECTO_PARTE_HORAS_PATRONES:
        hallados = search_read("project.project", [("name", "ilike", patron)], ["id", "name"])
        if hallados.empty:
            continue
        nuevos = hallados[~hallados["id"].isin(vistos)]
        if not nuevos.empty:
            vistos.update(int(i) for i in nuevos["id"].tolist())
            partes.append(nuevos)
    if partes:
        return pd.concat(partes, ignore_index=True), None

    domain_tareas: list = []
    codigos = list(CODIGOS_HORAS_GC.keys())
    for _ in range(len(codigos) - 1):
        domain_tareas.append("|")
    for codigo in codigos:
        domain_tareas.append(("name", "ilike", codigo))
    try:
        tareas = search_read("project.task", domain_tareas, ["id", "name", "project_id"])
    except Exception as e:
        return pd.DataFrame(columns=["id", "name"]), (
            f"No se encontró el proyecto de Gestión comercial ni se pudieron "
            f"leer las tareas {', '.join(codigos)}: {e}"
        )
    if tareas.empty:
        return pd.DataFrame(columns=["id", "name"]), (
            "No se encontró el proyecto 'Gestión comercial' (project.project) ni "
            f"tareas cuyo nombre contenga {', '.join(codigos)}. GC3/GC5/GC6/GC8 "
            "son tareas dentro de ese proyecto, no el nombre del proyecto."
        )
    proyecto_ids = [i for i in m2o_id(tareas["project_id"]).dropna().unique().tolist()]
    proyectos = search_read("project.project", [("id", "in", proyecto_ids)], ["id", "name"])
    if proyectos.empty:
        return pd.DataFrame(columns=["id", "name"]), (
            "Se encontraron tareas GC3/GC5/GC6/GC8 pero sin proyecto asociado."
        )
    return proyectos, None


@st.cache_data(ttl=600, show_spinner="Cargando parte de horas Gestión comercial...")
def load_actividades_gc14(date_from: str, date_to: str, team_ids: list[int]):
    """Parte de horas del proyecto Gestión comercial. GC3/GC5/GC6/GC8 son
    tareas (task_id) dentro de ese proyecto, no el proyecto. El vendedor se
    cruza employee_id → hr.employee.user_id → roster TD."""
    cols = ["date", "name", "task_id", "employee_id", "unit_amount", "project_id"]
    proyectos, err_proy = load_proyecto_gestion_comercial()
    if err_proy:
        return pd.DataFrame(columns=cols), err_proy
    if proyectos.empty:
        return pd.DataFrame(columns=cols), (
            "No se encontró el proyecto de Gestión comercial en Odoo."
        )
    proyecto_ids = proyectos["id"].tolist()
    domain = [
        ("project_id", "in", proyecto_ids),
        ("date", ">=", date_from),
        ("date", "<=", date_to),
    ]
    try:
        df = search_read("account.analytic.line", domain, cols, order="date")
    except Exception as e:
        nombres = ", ".join(proyectos["name"].astype(str).tolist())
        return pd.DataFrame(columns=cols), (
            f"No se pudo consultar el parte de horas (account.analytic.line) del "
            f"proyecto {nombres}: {e}"
        )
    if df.empty:
        return df, None

    roster = load_td_vendedores(team_ids)
    if roster.empty:
        return pd.DataFrame(columns=cols), (
            "No se encontró el roster de vendedores de TD (crm.team.member_ids) para "
            "cruzar contra el parte de horas."
        )

    empleado_ids = [i for i in m2o_id(df["employee_id"]).dropna().unique().tolist()]
    empleados = (search_read("hr.employee", [("id", "in", empleado_ids)], ["id", "user_id"])
                 if empleado_ids else pd.DataFrame(columns=["id", "user_id"]))
    if not empleados.empty:
        empleados["user_id_num"] = m2o_id(empleados["user_id"])
        empleado_a_user = empleados.set_index("id")["user_id_num"]
    else:
        empleado_a_user = pd.Series(dtype=object)

    df["date"] = pd.to_datetime(df["date"])
    df["empleado_id_num"] = m2o_id(df["employee_id"])
    df["user_id_num"] = df["empleado_id_num"].map(empleado_a_user)
    df["tarea"] = m2o_name(df["task_id"])

    vendedores_td = roster.set_index("user_id")["vendedor"]
    df["vendedor"] = df["user_id_num"].map(vendedores_td)
    df = df[df["vendedor"].notna()].copy()
    df["tipo_brief"] = df["tarea"].apply(clasificar_tarea_gc)
    return filtrar_vendedores_excluidos(df), None


@st.cache_data(ttl=600, show_spinner="Cargando costos analíticos por línea...")
def load_analytic_costs_by_linea(date_from: str, date_to: str):
    """Costos del equipo por línea de negocio, desde account.analytic.line: una
    cuenta analítica por línea, según LINEA_CUENTA_ANALITICA (el nombre de la
    cuenta no siempre coincide con el nombre de la línea), quedándonos solo con
    los movimientos de costo (importe negativo — convención estándar de Odoo:
    ingresos positivos, egresos negativos)."""
    cols = ["date", "amount", "account_id"]
    nombres_cuentas = list(LINEA_CUENTA_ANALITICA.values())
    domain = [
        ("account_id.name", "in", nombres_cuentas),
        ("date", ">=", date_from),
        ("date", "<=", date_to),
        ("amount", "<", 0),
    ]
    try:
        df = search_read("account.analytic.line", domain, cols)
    except Exception as e:
        return pd.DataFrame(columns=cols), (
            f"No se pudo consultar costos analíticos (account.analytic.line): {e}. "
            f"Verifica que existan las cuentas analíticas {', '.join(nombres_cuentas)} "
            f"en Odoo."
        )
    if df.empty:
        return df, None
    df["date"] = pd.to_datetime(df["date"])
    df["linea"] = m2o_name(df["account_id"]).map(CUENTA_ANALITICA_LINEA)
    df["mes"] = df["date"].dt.to_period("M").astype(str)
    df["costo"] = -df["amount"]
    return df, None


def construir_pivote_vendedor(datos: pd.DataFrame, value_col: str, year: int,
                               metas_vendedor: pd.DataFrame) -> pd.DataFrame:
    """Igual que construir_pivote_cumplimiento pero por vendedor en vez de línea
    (filas = vendedor + TOTAL, columnas = meses + Acumulado + % Cumpl. Anual)."""
    vendedores = metas_vendedor["vendedor"].tolist()
    filas = []
    for vendedor in vendedores:
        meta_row = metas_vendedor.loc[metas_vendedor["vendedor"] == vendedor, "meta_anual"]
        meta_anual = float(meta_row.iloc[0]) if not meta_row.empty else 0.0
        fila = {"Vendedor": vendedor}
        acumulado = 0.0
        for m, nombre_mes in enumerate(MESES_ES, start=1):
            mes_key = f"{year}-{m:02d}"
            valor = datos.loc[
                (datos["vendedor"] == vendedor) & (datos["mes"] == mes_key), value_col].sum()
            fila[nombre_mes] = valor
            acumulado += valor
        fila["Acumulado"] = acumulado
        fila["% Cumpl. Anual"] = (acumulado / meta_anual * 100) if meta_anual else 0.0
        fila["_meta_anual"] = meta_anual
        filas.append(fila)

    tabla = pd.DataFrame(filas)
    total = {"Vendedor": "TOTAL"}
    for nombre_mes in MESES_ES:
        total[nombre_mes] = tabla[nombre_mes].sum()
    total["Acumulado"] = tabla["Acumulado"].sum()
    meta_total = tabla["_meta_anual"].sum()
    total["% Cumpl. Anual"] = (total["Acumulado"] / meta_total * 100) if meta_total else 0.0
    tabla = tabla.drop(columns="_meta_anual")
    return pd.concat([tabla, pd.DataFrame([total])], ignore_index=True)


def fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def _fmt_bar_label(v, kind: str = "count") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    num = float(v)
    if kind == "money":
        return fmt_money(num)
    if kind == "hours":
        return f"{num:,.1f}"
    if kind == "pct":
        return f"{num:.1f}%"
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:,.1f}"


def show_bar_labels(fig, kind: str = "count", text_size: int | None = None):
    """Pone el valor al final de cada barra (fuera del trazo) para lectura rápida.

    text_size deja todas las etiquetas al mismo tamaño. Sin eso, Plotly reduce la
    fuente para que el texto quepa en el segmento y las barras bajas se ven diminutas.
    """
    max_pos, max_neg = 0.0, 0.0
    has_h = False
    for tr in fig.data:
        if getattr(tr, "type", None) != "bar":
            continue
        orientation = getattr(tr, "orientation", None) or "v"
        vals = list(tr.x if orientation == "h" else tr.y)
        tr.text = [_fmt_bar_label(v, kind) for v in vals]
        tr.textposition = "outside"
        tr.cliponaxis = False
        if text_size:
            # "none" evita el scale() que achica el texto; 0 evita que Plotly
            # rote las etiquetas que no caben a lo ancho de la barra.
            tr.constraintext = "none"
            tr.textangle = 0
            tr.textfont = dict(size=text_size)
        for v in vals:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                continue
            num = float(v)
            max_pos = max(max_pos, num)
            max_neg = min(max_neg, num)
        if orientation == "h":
            has_h = True
    if has_h and max_pos > 0:
        fig.update_xaxes(range=[min(0, max_neg * 1.15), max_pos * 1.18])
    elif max_pos > 0 or max_neg < 0:
        fig.update_yaxes(range=[min(0, max_neg * 1.15), max_pos * 1.18])
    return fig


def add_stack_totals(fig, df: pd.DataFrame, x: str, y: str, kind: str = "count"):
    """Anota el total encima de cada barra apilada (suma de segmentos por categoría x)."""
    if df is None or df.empty or x not in df.columns or y not in df.columns:
        return fig
    totales = df.groupby(x, sort=False)[y].sum()
    if totales.empty:
        return fig
    for cat, total in totales.items():
        fig.add_annotation(
            x=cat,
            y=float(total),
            text=_fmt_bar_label(total, kind),
            showarrow=False,
            yshift=14,
            font=dict(size=11, color="#374151"),
            bgcolor="rgba(255,255,255,0.75)",
            borderpad=2,
        )
    ymax = float(totales.max())
    if ymax > 0:
        fig.update_layout(yaxis=dict(range=[0, ymax * 1.15]))
    return fig


def fmt_miles_apostrofe(v: float) -> str:
    """Valor en miles de pesos, con apóstrofe como separador (p. ej. 40'463 para
    $40.463.000), al estilo de notación informal colombiana."""
    return f"{v / 1000:,.0f}".replace(",", "'")


def meses_ultimos_n(hoy: date, n: int = 12) -> list[str]:
    periodo = pd.Period(hoy, freq="M")
    return [(periodo - i).strftime("%Y-%m") for i in range(n - 1, -1, -1)]


def tabla_actividades_mes_a_mes(df: pd.DataFrame, extra: str, meses: list[str],
                                 extras: list[str] | None = None) -> pd.DataFrame:
    """Pivote mes × extra × 4 tipos del brief, con los 12 meses aunque vayan en 0."""
    brief = df[df["tipo_brief"].isin(TIPOS_ACTIVIDAD)] if not df.empty else df
    if extras is None:
        extras = (sorted(brief[extra].dropna().unique().tolist())
                  if not brief.empty and extra in brief.columns else [])
    if not extras:
        extras = ["—"]
    idx = pd.MultiIndex.from_product([meses, extras], names=["mes", extra])
    if brief.empty:
        piv = pd.DataFrame(0, index=idx, columns=TIPOS_ACTIVIDAD)
    else:
        conteo = (brief.groupby(["mes", extra, "tipo_brief"], as_index=False)
                  .agg(cantidad=("tipo", "count")))
        piv = conteo.pivot_table(index=["mes", extra], columns="tipo_brief",
                                 values="cantidad", aggfunc="sum", fill_value=0)
        for tipo in TIPOS_ACTIVIDAD:
            if tipo not in piv.columns:
                piv[tipo] = 0
        piv = piv[TIPOS_ACTIVIDAD].reindex(idx, fill_value=0)
    piv["Total"] = piv[TIPOS_ACTIVIDAD].sum(axis=1)
    return piv.reset_index()


# ─────────────────────────────────────────────
# Sidebar: filtros globales
# ─────────────────────────────────────────────
st.sidebar.title("⚙️ Filtros")

hoy = date.today()
rango = st.sidebar.date_input(
    "Rango de fechas",
    value=(hoy.replace(day=1) - timedelta(days=90), hoy),
    max_value=hoy,
)
if isinstance(rango, tuple) and len(rango) == 2:
    date_from, date_to = rango
else:
    st.stop()

teams_df = load_teams()
teams_td_df = teams_df[teams_df["name"].apply(lambda n: NOMBRE_EQUIPO_TD in _normalizar(n))]

if teams_td_df.empty:
    st.sidebar.error(
        f"⚠️ No se encontró en Odoo (crm.team) ningún equipo de ventas llamado "
        f"'{NOMBRE_EQUIPO_TD}'. El dashboard es exclusivo de esa línea de negocio, así "
        "que no puede continuar sin ubicarlo — revisa el nombre exacto del equipo en Odoo."
    )
    st.stop()

team_names = st.sidebar.multiselect(
    "Equipos de venta (Transformación Digital)",
    options=teams_td_df["name"].tolist(),
    default=[],
    placeholder="Todos los equipos de Transformación Digital",
)
seleccionados = teams_td_df.loc[teams_td_df["name"].isin(team_names), "id"].tolist()
# team_ids nunca queda vacío: sin selección, cae a TODOS los equipos de TD (nunca a
# "todos los equipos de Odoo", que incluiría otras líneas de negocio de Firefly).
team_ids = seleccionados if seleccionados else teams_td_df["id"].tolist()

if st.sidebar.button("🔄 Refrescar datos"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.caption(f"Datos cacheados por 10 min · {datetime.now():%H:%M}")

# ─────────────────────────────────────────────
# Carga
# ─────────────────────────────────────────────
d1, d2 = date_from.isoformat(), date_to.isoformat()
sales = load_sales(d1, d2, team_ids)
invoices = load_invoices(d1, d2, team_ids)
leads = load_leads(d1, d2, team_ids)
leads_full = load_leads_full(d1, d2, team_ids)
closed = load_pipeline_closed(d1, d2, team_ids)

st.title("📊 Dashboard Transformación Digital")
st.caption(f"Período: {date_from:%d/%m/%Y} → {date_to:%d/%m/%Y}"
           + (f" · Equipos: {', '.join(team_names)}" if team_names
              else " · Todos los equipos de Transformación Digital"))

(tab_td, tab_lineas, tab_metas, tab_fact, tab_ventas, tab_clientes, tab_equipo,
 tab_crm, tab_leads, tab_soporte) = st.tabs(
    ["🏠 Resumen", "🏷️ Metas por línea", "🏆 Metas por vendedor",
     "🧾 Facturación", "🛒 Ventas", "🧑‍🤝‍🧑 Clientes",
     "🕐 Horas y Actividad",
     "🎯 CRM / Pipeline", "📨 Leads", "🛠️ Soporte"]
)
# Pestaña "💵 Rentabilidad" oculta temporalmente (código conservado abajo).

# ─────────────────────────────────────────────
# TAB: Resumen Ejecutivo (KPIs consolidados de la línea)
# ─────────────────────────────────────────────
with tab_td:
    st.caption("Resumen ejecutivo consolidado: Proyectos (Implementación Odoo), Soporte "
               "y Licenciamiento — los tres tipos de ingreso de la línea. Esta pestaña NO "
               "usa el rango de fechas del sidebar: el año se elige abajo, y el 'mes "
               "actual' es siempre el mes calendario de hoy.")

    anio_td = st.selectbox("Año a analizar", options=range(hoy.year, hoy.year - 4, -1),
                           index=0, key="anio_td")
    metas_lineas_td = load_metas_lineas()

    if metas_lineas_td.empty:
        st.warning("No se encontró `data/metas_lineas.csv` con las metas por línea.")
    else:
        datos_td = load_cumplimiento_mensual(anio_td, team_ids)
        meta_anual_td = metas_lineas_td["meta_anual"].sum()
        vendido_anual_td = datos_td["vendido"].sum()
        facturado_anual_td = datos_td["facturado"].sum()
        pct_anual_td = (facturado_anual_td / meta_anual_td * 100) if meta_anual_td else 0
        meta_a_la_fecha_td = meta_prorateada_a_hoy(meta_anual_td, anio_td, hoy)
        pct_a_la_fecha_td = (facturado_anual_td / meta_a_la_fecha_td * 100) if meta_a_la_fecha_td else 0

        st.markdown("#### 🎯 Resumen del año")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Meta de ventas año", fmt_money(meta_anual_td))
        c2.metric("Ventas facturadas del año (sin IVA)", fmt_money(vendido_anual_td))
        c3.metric("Facturación acumulada del año", fmt_money(facturado_anual_td))
        c4.metric("% cumplimiento anual", f"{pct_anual_td:.1f}%")
        c5.metric("% cumplimiento a la fecha", f"{pct_a_la_fecha_td:.1f}%",
                  help="Contra la meta prorateada según los días transcurridos del año "
                       "(no la meta anual completa): 100% = vamos exactamente como se "
                       "esperaba a estas alturas del año.")

        if anio_td == hoy.year:
            st.markdown("#### 📅 Mes actual")
            mes_actual_key = f"{hoy.year}-{hoy.month:02d}"
            won_mes = load_won_lineas(hoy.replace(day=1).isoformat(), hoy.isoformat(), team_ids)
            won_mes_td = won_mes[won_mes["linea"].isin(LINEA_CATEGORIA)]
            ventas_mes = won_mes_td["expected_revenue"].sum()
            ticket_prom_mes = won_mes_td["expected_revenue"].mean() if not won_mes_td.empty else 0
            fact_mes = datos_td.loc[datos_td["mes"] == mes_actual_key, "facturado"].sum()

            c5, c6, c7 = st.columns(3)
            c5.metric("Ventas del mes", fmt_money(ventas_mes))
            c6.metric("Facturación del mes", fmt_money(fact_mes))
            c7.metric("Ticket promedio de venta", fmt_money(ticket_prom_mes) if ticket_prom_mes else "—")
        else:
            st.caption(f"La sección de 'mes actual' solo aplica al año en curso ({hoy.year}).")

        st.divider()
        st.markdown("#### 🛠️ Contratos de soporte activos")
        estandar_ct, estabilizacion_ct, err_ct = load_active_support_contracts()
        if err_ct:
            st.warning(err_ct)
        else:
            meta_contratos = load_meta_contratos()
            n_activos = len(estandar_ct)
            n_estab = len(estabilizacion_ct)
            pct_contratos = (n_activos / meta_contratos * 100) if meta_contratos else 0

            c8, c9, c10 = st.columns(3)
            c8.metric("Contratos de soporte activos", n_activos)
            c9.metric("Meta de contratos (número)",
                      f"{meta_contratos:.0f}" if meta_contratos else "Sin definir")
            c10.metric("% cumplimiento",
                       f"{pct_contratos:.1f}%" if meta_contratos else "—")
            if not meta_contratos:
                st.caption("Define la meta en `data/metas_contratos.csv` "
                          "(columna `meta_contratos_anual`) para ver el % de cumplimiento.")
            if n_estab:
                st.caption(f"➕ {n_estab} cliente(s) con contrato de Estabilización activo "
                          "(seguimiento informativo, sin límites de tickets/horas — no "
                          "cuentan en el total de arriba).")
            with st.expander("📋 Detalle de contratos activos"):
                st.dataframe(
                    estandar_ct[["name", "support_contract_ref", "support_contract_state",
                                 "support_contract_start", "support_contract_end",
                                 "support_ticket_limit", "support_hours_limit"]],
                    use_container_width=True, hide_index=True,
                    column_config={
                        "name": "Cliente", "support_contract_ref": "Referencia",
                        "support_contract_state": "Estado",
                        "support_contract_start": "Inicio", "support_contract_end": "Vence",
                        "support_ticket_limit": "Tickets/mes", "support_hours_limit": "Horas/mes",
                    },
                )

        st.divider()
        st.markdown("#### 📈 Proyectos y contratos de soporte vendidos — últimos 12 meses")
        st.caption("Cantidad de oportunidades GANADAS por mes, según etiqueta de línea "
                   "(no depende del año seleccionado arriba). La etiqueta de cada barra "
                   "muestra Cantidad / Valor vendido (en miles de pesos).")

        desde_12m = (pd.Period(hoy, freq="M") - 11).to_timestamp().date()
        won_12m = load_won_lineas(desde_12m.isoformat(), hoy.isoformat(), team_ids)

        col_a, col_b = st.columns(2)
        with col_a:
            proyectos_12m = won_12m[won_12m["linea"] == "Implementación Odoo"] if not won_12m.empty else won_12m
            if proyectos_12m.empty:
                st.info("No hay proyectos ganados en los últimos 12 meses.")
            else:
                conteo = (proyectos_12m.groupby("mes", as_index=False)
                          .agg(proyectos=("name", "count"),
                               valor=("expected_revenue", "sum")))
                conteo["etiqueta"] = conteo.apply(
                    lambda r: f"{r['proyectos']}/ {fmt_miles_apostrofe(r['valor'])}", axis=1)
                fig = px.bar(conteo, x="mes", y="proyectos", text="etiqueta",
                             title="Proyectos vendidos por mes",
                             labels={"proyectos": "Proyectos", "mes": "Mes"})
                fig.update_traces(textposition="outside")
                st.plotly_chart(fig, use_container_width=True, key="chart_1")
        with col_b:
            soporte_12m = won_12m[won_12m["linea"] == "Soporte y Mantenimiento"] if not won_12m.empty else won_12m
            if soporte_12m.empty:
                st.info("No hay contratos de soporte ganados en los últimos 12 meses.")
            else:
                conteo_s = (soporte_12m.groupby("mes", as_index=False)
                            .agg(contratos=("name", "count"),
                                 valor=("expected_revenue", "sum")))
                conteo_s["etiqueta"] = conteo_s.apply(
                    lambda r: f"{r['contratos']}/ {fmt_miles_apostrofe(r['valor'])}", axis=1)
                fig = px.bar(conteo_s, x="mes", y="contratos", text="etiqueta",
                             title="Contratos de soporte vendidos por mes",
                             labels={"contratos": "Contratos", "mes": "Mes"})
                fig.update_traces(textposition="outside")
                st.plotly_chart(fig, use_container_width=True, key="chart_2")

        st.divider()
        st.markdown("#### 🧾 Facturación mensual por tipo (Proyectos / Soporte / Licenciamiento)")
        fig = px.bar(datos_td, x="mes", y="facturado", color="linea", barmode="group",
                     title=f"Facturado por mes y línea — {anio_td}",
                     labels={"facturado": "COP", "mes": "Mes", "linea": "Línea"})
        show_bar_labels(fig, kind="money")
        st.plotly_chart(fig, use_container_width=True, key="chart_3")

        st.markdown("#### 🏆 Meta del año y total facturado por tipo")
        resumen_tipo = metas_lineas_td.merge(
            datos_td.groupby("linea", as_index=False)["facturado"].sum(),
            on="linea", how="left")
        resumen_tipo["facturado"] = resumen_tipo["facturado"].fillna(0)
        resumen_tipo["pct"] = resumen_tipo.apply(
            lambda r: r["facturado"] / r["meta_anual"] * 100 if r["meta_anual"] else 0, axis=1)
        resumen_tipo["meta_a_la_fecha"] = resumen_tipo["meta_anual"].apply(
            lambda m: meta_prorateada_a_hoy(m, anio_td, hoy))
        resumen_tipo["pct_a_la_fecha"] = resumen_tipo.apply(
            lambda r: r["facturado"] / r["meta_a_la_fecha"] * 100 if r["meta_a_la_fecha"] else 0, axis=1)
        st.dataframe(
            resumen_tipo[["linea", "meta_anual", "facturado", "pct", "pct_a_la_fecha"]],
            use_container_width=True, hide_index=True,
            column_config={
                "linea": "Línea",
                "meta_anual": st.column_config.NumberColumn("Meta año", format="$%,.0f"),
                "facturado": st.column_config.NumberColumn("Total facturado", format="$%,.0f"),
                "pct": st.column_config.ProgressColumn("% cumplimiento anual", format="%.1f%%",
                                                        min_value=0, max_value=150),
                "pct_a_la_fecha": st.column_config.ProgressColumn(
                    "% cumplimiento a la fecha", format="%.1f%%", min_value=0, max_value=150),
            },
        )

        st.divider()
        st.markdown("#### 🏅 Top 10 vendedores y clientes")
        st.caption("Facturado = `account.invoice.report` en COP sin IVA. CRM ganados = "
                   "oportunidades `won` del año (cantidad y valor esperado).")
        fact_anio_top = load_invoice_report(f"{anio_td}-01-01", f"{anio_td}-12-31", team_ids)
        won_anio_top = load_won(f"{anio_td}-01-01", f"{anio_td}-12-31", team_ids)

        col_tv1, col_tv2 = st.columns(2)
        with col_tv1:
            if fact_anio_top.empty:
                st.info("No hay facturación en el año.")
            else:
                top_v_fact = (fact_anio_top.groupby("vendedor", as_index=False)["monto"]
                              .sum().sort_values("monto", ascending=False).head(10))
                fig = px.bar(top_v_fact.sort_values("monto"), x="monto", y="vendedor",
                             orientation="h", title=f"Top 10 vendedores por facturado — {anio_td}",
                             text_auto=".2s", labels={"monto": "COP sin IVA", "vendedor": ""})
                show_bar_labels(fig, kind="money")
                st.plotly_chart(fig, use_container_width=True, key="chart_4")
                st.dataframe(
                    top_v_fact, use_container_width=True, hide_index=True,
                    column_config={
                        "vendedor": "Vendedor",
                        "monto": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                    },
                )
        with col_tv2:
            if won_anio_top.empty:
                st.info("No hay CRM ganados en el año.")
            else:
                top_v_crm = (won_anio_top.groupby("vendedor", as_index=False)
                             .agg(ganados=("name", "count"), valor=("expected_revenue", "sum"))
                             .sort_values("ganados", ascending=False).head(10))
                fig = px.bar(top_v_crm.sort_values("ganados"), x="ganados", y="vendedor",
                             orientation="h", title=f"Top 10 vendedores por CRM ganados — {anio_td}",
                             text_auto=True, labels={"ganados": "Oportunidades", "vendedor": ""})
                show_bar_labels(fig, kind="count")
                st.plotly_chart(fig, use_container_width=True, key="chart_5")
                st.dataframe(
                    top_v_crm, use_container_width=True, hide_index=True,
                    column_config={
                        "vendedor": "Vendedor",
                        "ganados": "Ganados",
                        "valor": st.column_config.NumberColumn("Valor CRM", format="$%,.0f"),
                    },
                )

        col_tc1, col_tc2 = st.columns(2)
        with col_tc1:
            if fact_anio_top.empty:
                st.info("No hay facturación en el año.")
            else:
                top_c_fact = (fact_anio_top.groupby("cliente", as_index=False)["monto"]
                              .sum().sort_values("monto", ascending=False).head(10))
                fig = px.bar(top_c_fact.sort_values("monto"), x="monto", y="cliente",
                             orientation="h", title=f"Top 10 clientes por facturado — {anio_td}",
                             text_auto=".2s", labels={"monto": "COP sin IVA", "cliente": ""})
                show_bar_labels(fig, kind="money")
                st.plotly_chart(fig, use_container_width=True, key="chart_6")
                st.dataframe(
                    top_c_fact, use_container_width=True, hide_index=True,
                    column_config={
                        "cliente": "Cliente",
                        "monto": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                    },
                )
        with col_tc2:
            if won_anio_top.empty:
                st.info("No hay CRM ganados en el año.")
            else:
                top_c_crm = (won_anio_top.groupby("cliente", as_index=False)
                             .agg(ganados=("name", "count"), valor=("expected_revenue", "sum"))
                             .sort_values("ganados", ascending=False).head(10))
                fig = px.bar(top_c_crm.sort_values("ganados"), x="ganados", y="cliente",
                             orientation="h", title=f"Top 10 clientes por CRM ganados — {anio_td}",
                             text_auto=True, labels={"ganados": "Oportunidades", "cliente": ""})
                show_bar_labels(fig, kind="count")
                st.plotly_chart(fig, use_container_width=True, key="chart_7")
                st.dataframe(
                    top_c_crm, use_container_width=True, hide_index=True,
                    column_config={
                        "cliente": "Cliente",
                        "ganados": "Ganados",
                        "valor": st.column_config.NumberColumn("Valor CRM", format="$%,.0f"),
                    },
                )

# ─────────────────────────────────────────────
# TAB 1: Ventas (sale.order)
# ─────────────────────────────────────────────
with tab_ventas:
    st.caption("Montos de órdenes de venta **antes de impuestos** "
               "(`sale.order.amount_untaxed`).")
    if sales.empty:
        st.info("No hay órdenes de venta confirmadas en el período seleccionado.")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Órdenes confirmadas", len(sales))
        c2.metric("Ventas (sin IVA)", fmt_money(sales["amount_untaxed"].sum()))
        c3.metric("Ticket promedio (sin IVA)", fmt_money(sales["amount_untaxed"].mean()))

        col_a, col_b = st.columns(2)
        with col_a:
            por_equipo = (sales.groupby("equipo", as_index=False)["amount_untaxed"]
                          .sum().sort_values("amount_untaxed", ascending=False))
            fig = px.bar(por_equipo, x="equipo", y="amount_untaxed",
                         title="Ventas por equipo (sin IVA)", text_auto=".2s",
                         labels={"amount_untaxed": "COP sin IVA", "equipo": "Equipo"})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_8")
        with col_b:
            mensual = (sales.assign(mes=sales["date_order"].dt.to_period("M").astype(str))
                       .groupby(["mes", "equipo"], as_index=False)["amount_untaxed"].sum())
            fig = px.line(mensual, x="mes", y="amount_untaxed", color="equipo",
                          markers=True, title="Evolución mensual por equipo (sin IVA)",
                          labels={"amount_untaxed": "COP sin IVA", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True, key="chart_9")

        col_c, col_d = st.columns(2)
        with col_c:
            por_vendedor = (sales.groupby("vendedor", as_index=False)["amount_untaxed"]
                            .sum().sort_values("amount_untaxed", ascending=True).tail(10))
            fig = px.bar(por_vendedor, x="amount_untaxed", y="vendedor", orientation="h",
                         title="Top 10 vendedores (sin IVA)", text_auto=".2s",
                         labels={"amount_untaxed": "COP sin IVA", "vendedor": ""})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_10")
        with col_d:
            top_clientes = (sales.groupby("cliente", as_index=False)["amount_untaxed"]
                            .sum().sort_values("amount_untaxed", ascending=True).tail(10))
            fig = px.bar(top_clientes, x="amount_untaxed", y="cliente", orientation="h",
                         title="Top 10 clientes (sin IVA)", text_auto=".2s",
                         labels={"amount_untaxed": "COP sin IVA", "cliente": ""})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_11")

        mensual_vendedor = (sales.assign(mes=sales["date_order"].dt.to_period("M").astype(str))
                            .groupby(["mes", "vendedor"], as_index=False)["amount_untaxed"].sum())
        fig = px.bar(mensual_vendedor, x="mes", y="amount_untaxed", color="vendedor",
                     barmode="group", title="Ventas por vendedor y mes (sin IVA)",
                     labels={"amount_untaxed": "COP sin IVA", "mes": "Mes"})
        show_bar_labels(fig, kind="money")
        st.plotly_chart(fig, use_container_width=True, key="chart_12")

        with st.expander("📋 Detalle de órdenes"):
            st.dataframe(
                sales[["name", "date_order", "cliente", "vendedor", "equipo",
                       "amount_untaxed"]],
                use_container_width=True, hide_index=True,
                column_config={
                    "amount_untaxed": st.column_config.NumberColumn(
                        "Monto (sin IVA)", format="$%,.0f"),
                },
            )

    st.divider()
    st.markdown("#### 📅 Últimos 12 meses (fijo, no depende del rango del sidebar)")
    desde_12m_v = (pd.Period(hoy, freq="M") - 11).to_timestamp().date()
    sales_12m = load_sales(desde_12m_v.isoformat(), hoy.isoformat(), team_ids)
    won_12m_v = load_won(desde_12m_v.isoformat(), hoy.isoformat(), team_ids)

    col_v12a, col_v12b = st.columns(2)
    with col_v12a:
        if sales_12m.empty:
            st.info("No hay ventas en los últimos 12 meses.")
        else:
            mensual_vendedor_12m = (sales_12m.assign(
                mes=sales_12m["date_order"].dt.to_period("M").astype(str))
                .groupby(["mes", "vendedor"], as_index=False)["amount_untaxed"].sum())
            fig = px.bar(mensual_vendedor_12m, x="mes", y="amount_untaxed", color="vendedor",
                         barmode="group",
                         title="Ventas por vendedor y mes — últimos 12 meses (sin IVA)",
                         labels={"amount_untaxed": "COP sin IVA", "mes": "Mes"})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_13")
    with col_v12b:
        if won_12m_v.empty:
            st.info("No hay oportunidades cerradas (ganadas) en los últimos 12 meses.")
        else:
            cierres_vendedor_12m = (won_12m_v.groupby(["mes", "vendedor"], as_index=False)
                                     .agg(oportunidades=("name", "count")))
            fig = px.bar(cierres_vendedor_12m, x="mes", y="oportunidades", color="vendedor",
                         barmode="group",
                         title="Oportunidades cerradas (ganadas) por vendedor y mes — últimos 12 meses",
                         labels={"oportunidades": "Oportunidades", "mes": "Mes"})
            show_bar_labels(fig, kind="count")
            st.plotly_chart(fig, use_container_width=True, key="chart_14")

# ─────────────────────────────────────────────
# TAB 2: Facturación (account.move + Aged Receivable)
# ─────────────────────────────────────────────
with tab_fact:
    st.caption("Facturación **antes de impuestos** (`account.invoice.report.price_subtotal`). "
               "Misma lógica que el INFORME / Análisis de Facturas: facturas de cliente "
               "no borrador/canceladas, **sin movimiento de reversión**, categoría exacta.")

    st.markdown("#### 📊 Facturación por línea (pivote Análisis de Facturas)")
    st.caption("Columnas = categorías **Licenciamiento / Soporte / Transformación digital** "
               "(subtotal sin IVA). Filas = meses del año. Equivalente al pivote de Odoo.")
    anio_fact_piv = st.selectbox(
        "Año del pivote", options=range(hoy.year, hoy.year - 4, -1),
        index=0, key="anio_facturacion_pivote")
    piv_fact = load_facturacion_pivote_anual(anio_fact_piv)
    st.dataframe(
        piv_fact,
        use_container_width=True, hide_index=True,
        column_config={
            "Mes": "Mes",
            "Licenciamiento": st.column_config.NumberColumn("Licenciamiento", format="$%,.2f"),
            "Soporte": st.column_config.NumberColumn("Soporte", format="$%,.2f"),
            "Transformación digital": st.column_config.NumberColumn(
                "Transformación digital", format="$%,.2f"),
            "Total": st.column_config.NumberColumn("Total", format="$%,.2f"),
        },
    )
    # Gráfico mensual apilado por categoría (sin la fila Total)
    piv_chart = piv_fact[piv_fact["Mes"] != "Total"].melt(
        id_vars="Mes",
        value_vars=["Licenciamiento", "Soporte", "Transformación digital"],
        var_name="categoria", value_name="monto")
    if not piv_chart.empty and piv_chart["monto"].sum() != 0:
        fig = px.bar(
            piv_chart, x="Mes", y="monto", color="categoria", barmode="stack",
            title=f"Facturado sin IVA por mes y categoría — {anio_fact_piv}",
            labels={"monto": "COP sin IVA", "Mes": "Mes", "categoria": "Categoría"},
        )
        add_stack_totals(fig, piv_chart, "Mes", "monto", kind="money")
        st.plotly_chart(fig, use_container_width=True, key="chart_fact_pivote")

    st.divider()
    st.markdown("#### Facturación del período (filtro del sidebar · por equipo TD)")
    st.caption("Cartera = Aged Receivable (incluye IVA). Lo facturado abajo es sin IVA.")

    if invoices.empty:
        st.info("No hay facturas publicadas en el período seleccionado.")
    else:
        facturado = invoices["amount_untaxed_signed"].sum()
        pagado_periodo = invoices.loc[
            invoices["payment_state"].isin(["paid", "reversed"]),
            "amount_untaxed_signed"].sum()
        nc = (invoices["move_type"] == "out_refund").sum()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Documentos", len(invoices))
        c2.metric("Facturado neto (sin IVA)", fmt_money(facturado))
        c3.metric("Pagado del período (sin IVA)", fmt_money(pagado_periodo),
                  help="Facturas/NC del rango del sidebar con payment_state "
                       "pagada o revertida. No entra en Aged Receivable.")
        c4.metric("Notas crédito", int(nc))

        col_a, col_b = st.columns(2)
        with col_a:
            por_equipo = (invoices.groupby("equipo", as_index=False)["amount_untaxed_signed"]
                          .sum().sort_values("amount_untaxed_signed", ascending=False))
            fig = px.bar(por_equipo, x="equipo", y="amount_untaxed_signed",
                         title="Facturación neta por equipo (sin IVA)", text_auto=".2s",
                         labels={"amount_untaxed_signed": "COP", "equipo": "Equipo"})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_15")
        with col_b:
            mensual = (invoices.assign(mes=invoices["invoice_date"].dt.to_period("M").astype(str))
                       .groupby(["mes", "equipo"], as_index=False)["amount_untaxed_signed"].sum())
            fig = px.bar(mensual, x="mes", y="amount_untaxed_signed", color="equipo",
                         barmode="group", title="Facturación mensual por equipo (sin IVA)",
                         labels={"amount_untaxed_signed": "COP", "mes": "Mes"})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_16")

        with st.expander("📋 Detalle de facturas del período"):
            st.dataframe(
                invoices[["name", "invoice_date", "invoice_date_due", "cliente",
                          "vendedor", "equipo", "amount_untaxed_signed",
                          "amount_residual_signed", "payment_state"]],
                use_container_width=True, hide_index=True,
                column_config={
                    "name": "Factura",
                    "invoice_date": "Fecha",
                    "invoice_date_due": "Vencimiento",
                    "amount_untaxed_signed": st.column_config.NumberColumn(
                        "Facturado (sin IVA)", format="$%,.0f"),
                    "amount_residual_signed": st.column_config.NumberColumn(
                        "Saldo por cobrar (con IVA)", format="$%,.0f"),
                    "payment_state": "Estado de pago",
                },
            )

    st.markdown("#### 📂 Cartera vigente — Aged Receivable")
    st.caption("Saldos abiertos a hoy (no el rango del sidebar). Al día = por "
               "pagar en tiempo (vencimiento ≥ hoy). 1-30 … +120 = vencido por "
               "antigüedad. Las facturas ya pagadas no aparecen. "
               "El saldo de cartera es lo que el cliente debe e **incluye IVA** "
               "(no comparable 1:1 con facturado sin impuestos).")
    cartera, err_cartera = load_aged_receivable(team_ids, hoy.isoformat())
    if err_cartera:
        st.warning(err_cartera)
    elif cartera.empty:
        st.info("No hay cartera abierta de Transformación Digital.")
    else:
        labels_cubo = dict(CUBOS_CARTERA)
        cartera_tabla = (cartera.groupby(["cliente", "factura", "invoice_date",
                                          "vencimiento", "cubo"], as_index=False)["saldo"].sum())
        pivote_c = (cartera_tabla.pivot_table(
            index=["cliente", "factura", "invoice_date", "vencimiento"],
            columns="cubo", values="saldo", aggfunc="sum", fill_value=0)
            .reset_index())
        for key, _nombre in CUBOS_CARTERA:
            if key not in pivote_c.columns:
                pivote_c[key] = 0.0
        pivote_c["total"] = sum(pivote_c[k] for k, _n in CUBOS_CARTERA)
        pivote_c = pivote_c.sort_values("total", ascending=False)

        al_dia = float(cartera.loc[cartera["cubo"] == "al_dia", "saldo"].sum())
        vencido = float(cartera.loc[cartera["cubo"] != "al_dia", "saldo"].sum())
        vigente = al_dia + vencido

        k1, k2, k3 = st.columns(3)
        k1.metric("Cartera vigente", fmt_money(vigente))
        k2.metric("Al día (por pagar en tiempo)", fmt_money(al_dia))
        k3.metric("Vencido", fmt_money(vencido))

        col_cfg_c = {
            "cliente": "Cliente",
            "factura": "Factura",
            "invoice_date": "Fecha factura",
            "vencimiento": "Vencimiento",
            "total": st.column_config.NumberColumn("Total", format="$%,.0f"),
        }
        mostrar = pivote_c[["cliente", "factura", "invoice_date", "vencimiento"]
                           + [k for k, _n in CUBOS_CARTERA] + ["total"]]
        rename_c = {k: n for k, n in CUBOS_CARTERA}
        mostrar = mostrar.rename(columns=rename_c)
        for _k, nombre in CUBOS_CARTERA:
            col_cfg_c[nombre] = st.column_config.NumberColumn(nombre, format="$%,.0f")
        st.dataframe(mostrar, use_container_width=True, hide_index=True,
                     column_config=col_cfg_c)

        cubos_sum = (cartera.groupby("cubo", as_index=False)["saldo"].sum())
        cubos_sum["cubo"] = cubos_sum["cubo"].map(labels_cubo).fillna(cubos_sum["cubo"])
        orden_cubos = [n for _k, n in CUBOS_CARTERA]
        cubos_sum["cubo"] = pd.Categorical(cubos_sum["cubo"], orden_cubos, ordered=True)
        cubos_sum = cubos_sum.sort_values("cubo")
        fig = px.bar(cubos_sum, x="cubo", y="saldo", text_auto=".2s",
                     title="Cartera por antigüedad (Aged Receivable)",
                     labels={"cubo": "Cubo", "saldo": "COP"})
        show_bar_labels(fig, kind="money")
        st.plotly_chart(fig, use_container_width=True, key="chart_17")

    st.divider()
    st.markdown("### 🎯 Cumplimiento Meta vs. Ventas facturadas por línea de negocio")
    st.caption("Año calendario completo (no el rango del sidebar). Las **ventas** del "
              "cumplimiento son lo **facturado antes de impuestos** "
              "(`account.invoice.report.price_subtotal`, misma fuente que Análisis de "
              "Facturas en Odoo 19), clasificado por categoría de producto.")

    anio_cumpl = st.selectbox("Año a analizar", options=range(hoy.year, hoy.year - 4, -1),
                              index=0, key="anio_cumplimiento_lineas")
    metas_lineas_cumpl = load_metas_lineas()

    if metas_lineas_cumpl.empty:
        st.warning("No se encontró `data/metas_lineas.csv` con las metas por línea.")
    else:
        datos_cumpl = load_cumplimiento_mensual(anio_cumpl, team_ids)
        meta_mensual_por_linea = metas_lineas_cumpl.set_index("linea")["meta_anual"] / 12
        datos_cumpl["meta_mensual"] = datos_cumpl["linea"].map(meta_mensual_por_linea).fillna(0)
        datos_cumpl["diferencia"] = datos_cumpl["facturado"] - datos_cumpl["meta_mensual"]

        meta_anual_total = metas_lineas_cumpl["meta_anual"].sum()
        facturado_total = datos_cumpl["facturado"].sum()
        pct_anual_total = (facturado_total / meta_anual_total * 100) if meta_anual_total else 0
        meta_a_la_fecha_total = meta_prorateada_a_hoy(meta_anual_total, anio_cumpl, hoy)
        pct_a_la_fecha_total = (facturado_total / meta_a_la_fecha_total * 100) if meta_a_la_fecha_total else 0
        diferencia_total = facturado_total - meta_anual_total

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Meta anual (3 líneas)", fmt_money(meta_anual_total))
        c2.metric("Ventas facturadas (sin IVA)", fmt_money(facturado_total))
        c3.metric("% cumplimiento anual", f"{pct_anual_total:.1f}%")
        c4.metric("% cumplimiento a la fecha", f"{pct_a_la_fecha_total:.1f}%",
                  help="Compara lo facturado contra la meta prorateada según los días "
                       "transcurridos del año (no contra la meta anual completa): 100% "
                       "significa que vamos exactamente como se esperaba a estas alturas "
                       "del año.")
        c5.metric("Diferencia vs. meta anual", fmt_money(diferencia_total))

        with st.expander("📌 Metas anuales y mensuales por línea"):
            ref = metas_lineas_cumpl[metas_lineas_cumpl["linea"].isin(LINEA_CATEGORIA)].copy()
            ref["meta_mensual"] = ref["meta_anual"] / 12
            st.dataframe(
                ref[["linea", "meta_anual", "meta_mensual"]],
                use_container_width=True, hide_index=True,
                column_config={
                    "linea": "Línea",
                    "meta_anual": st.column_config.NumberColumn("Meta anual", format="$%,.0f"),
                    "meta_mensual": st.column_config.NumberColumn("Meta mensual", format="$%,.0f"),
                },
            )

        consolidado_mensual = datos_cumpl.groupby("mes", as_index=False)["facturado"].sum()
        consolidado_mensual["meta"] = meta_anual_total / 12
        consolidado_largo = consolidado_mensual.melt(
            id_vars="mes", value_vars=["meta", "facturado"],
            var_name="concepto", value_name="valor")
        consolidado_largo["concepto"] = consolidado_largo["concepto"].map(
            {"meta": "Meta mensual", "facturado": "Ventas facturadas (sin IVA)"})
        fig = px.bar(consolidado_largo, x="mes", y="valor", color="concepto", barmode="group",
                     title="Meta vs. Ventas facturadas por mes (consolidado, 3 líneas)",
                     color_discrete_map={"Meta mensual": "#9ca3af",
                                         "Ventas facturadas (sin IVA)": "#1f77b4"},
                     labels={"valor": "COP", "mes": "Mes"})
        show_bar_labels(fig, kind="money")
        st.plotly_chart(fig, use_container_width=True, key="chart_18")

        fig = px.bar(datos_cumpl, x="mes", y="facturado", color="linea", barmode="group",
                     title="Ventas facturadas por mes y línea de negocio (sin IVA)",
                     labels={"facturado": "COP", "mes": "Mes", "linea": "Línea"})
        show_bar_labels(fig, kind="money")
        st.plotly_chart(fig, use_container_width=True, key="chart_19")

        col_config_pivote = {"Línea": "Línea"}
        for m in MESES_ES:
            col_config_pivote[m] = st.column_config.NumberColumn(m, format="$%,.0f")
        col_config_pivote["Acumulado"] = st.column_config.NumberColumn("Acumulado", format="$%,.0f")
        col_config_pivote["% Cumpl. Anual"] = st.column_config.ProgressColumn(
            "% Cumpl. Anual", format="%.1f%%", min_value=0, max_value=150)
        col_config_pivote["% Cumpl. a la Fecha"] = st.column_config.ProgressColumn(
            "% Cumpl. a la Fecha", format="%.1f%%", min_value=0, max_value=150)

        st.markdown("#### 🧾 Ventas facturadas (sin IVA) por línea y mes")
        pivote_facturado = construir_pivote_cumplimiento(datos_cumpl, "facturado", anio_cumpl, metas_lineas_cumpl, hoy)
        st.dataframe(pivote_facturado, use_container_width=True, hide_index=True,
                     column_config=col_config_pivote)

        st.markdown("#### ↕️ Diferencia mensual: Facturado vs. Meta, por línea")
        col_config_dif = {"Línea": "Línea"}
        for m in MESES_ES:
            col_config_dif[m] = st.column_config.NumberColumn(m, format="$%,.0f")
        col_config_dif["Acumulado"] = st.column_config.NumberColumn("Acumulado", format="$%,.0f")
        col_config_dif["% Cumpl. Anual"] = st.column_config.NumberColumn(
            "% variación vs. meta anual", format="%.1f%%")
        pivote_dif = construir_pivote_cumplimiento(datos_cumpl, "diferencia", anio_cumpl, metas_lineas_cumpl, hoy)
        st.dataframe(pivote_dif, use_container_width=True, hide_index=True,
                     column_config=col_config_dif)

        with st.expander("📋 Detalle mensual por línea (datos crudos)"):
            detalle = datos_cumpl.copy()
            detalle["pct_cumpl"] = detalle.apply(
                lambda r: r["facturado"] / r["meta_mensual"] * 100 if r["meta_mensual"] else 0, axis=1)
            st.dataframe(
                detalle[["linea", "mes", "meta_mensual", "facturado", "pct_cumpl", "diferencia"]]
                    .sort_values(["linea", "mes"]),
                use_container_width=True, hide_index=True,
                column_config={
                    "linea": "Línea", "mes": "Mes",
                    "meta_mensual": st.column_config.NumberColumn("Meta mensual", format="$%,.0f"),
                    "facturado": st.column_config.NumberColumn("Facturado (sin IVA)", format="$%,.0f"),
                    "pct_cumpl": st.column_config.ProgressColumn(
                        "% Cumpl.", format="%.1f%%", min_value=0, max_value=150),
                    "diferencia": st.column_config.NumberColumn("Diferencia", format="$%,.0f"),
                },
            )

# ─────────────────────────────────────────────
# TAB 3: CRM (crm.lead)
# ─────────────────────────────────────────────
with tab_crm:
    st.caption(
        "Pipeline abierto = leads/oportunidades **creadas** en el rango del sidebar que "
        "siguen abiertas (valor esperado). "
        "**Lo ganado** se toma por **fecha de cierre** (`date_closed`), no por fecha de "
        "creación: incluye oportunidades abiertas desde antes que se cerraron en este "
        "período. Se excluyen asuntos de correo tipo `Re:` / `Fw:` que Odoo crea como leads."
    )
    # Ganado real del período = cierre en el rango (misma fuente que Metas / Leads).
    won_crm = load_won(d1, d2, team_ids)
    closed_crm = closed  # load_pipeline_closed: ganadas+perdidas por date_closed

    if leads.empty and won_crm.empty:
        st.info("No hay leads/oportunidades creadas ni cierres en el período seleccionado.")
    else:
        abiertas = leads[leads["estado"] == "Abierta"] if not leads.empty else leads
        ganadas_cierre = (closed_crm[closed_crm["estado"] == "Ganada"]
                          if not closed_crm.empty else closed_crm)
        perdidas_cierre = (closed_crm[closed_crm["estado"] == "Perdida"]
                           if not closed_crm.empty else closed_crm)
        cerradas_n = len(ganadas_cierre) + len(perdidas_cierre)
        win_rate = len(ganadas_cierre) / cerradas_n * 100 if cerradas_n else 0
        sin_asignar = (leads[leads["vendedor"] == "Sin asignar"]
                       if not leads.empty else leads)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Creados en el período", len(leads))
        c2.metric("Pipeline abierto (valor esperado)",
                  fmt_money(abiertas["expected_revenue"].sum() if not abiertas.empty else 0))
        c3.metric("Ganado (fecha de cierre)",
                  fmt_money(won_crm["expected_revenue"].sum() if not won_crm.empty else 0),
                  help="Suma de expected_revenue de oportunidades con won_status=won "
                       "cuya date_closed cae en el rango del sidebar.")
        c4.metric("Tasa de conversión (por cierre)", f"{win_rate:.1f}%")
        c5.metric("Sin vendedor asignado", len(sin_asignar))

        if not sin_asignar.empty:
            with st.expander(f"⚠️ {len(sin_asignar)} sin vendedor asignado (tras excluir Re:/Fw:)"):
                st.dataframe(
                    sin_asignar[["name", "tipo", "create_date", "vendedor", "equipo", "etapa",
                                 "expected_revenue", "estado"]],
                    use_container_width=True, hide_index=True,
                    column_config={
                        "name": "Nombre",
                        "tipo": "Tipo",
                        "create_date": "Creado",
                        "expected_revenue": st.column_config.NumberColumn(
                            "Ingreso esperado", format="$%,.0f"),
                    },
                )

        col_a, col_b = st.columns(2)
        with col_a:
            if abiertas.empty:
                st.info("No hay pipeline abierto creado en el período.")
            else:
                embudo = (abiertas.groupby("etapa", as_index=False)
                          .agg(oportunidades=("name", "count"),
                               valor=("expected_revenue", "sum")))
                fig = px.funnel(embudo, x="valor", y="etapa",
                                title="Embudo abierto (valor esperado · creados en el período)")
                st.plotly_chart(fig, use_container_width=True, key="chart_20")
        with col_b:
            if leads.empty:
                st.info("No hay creados en el período.")
            else:
                por_equipo = (leads.groupby(["equipo", "estado"], as_index=False)
                              .agg(n=("name", "count")))
                fig = px.bar(por_equipo, x="equipo", y="n", color="estado",
                             title="Creados por equipo y estado (fecha de creación)",
                             color_discrete_map={"Ganada": "#2ca02c", "Perdida": "#d62728",
                                                 "Abierta": "#1f77b4"},
                             labels={"n": "Cantidad", "equipo": "Equipo"})
                show_bar_labels(fig, kind="count")
                st.plotly_chart(fig, use_container_width=True, key="chart_21")

        col_c, col_d = st.columns(2)
        with col_c:
            if abiertas.empty:
                st.info("Sin valor esperado abierto en el período.")
            else:
                por_vendedor_abierto = (abiertas.groupby("vendedor", as_index=False)
                                        .agg(valor=("expected_revenue", "sum"))
                                        .sort_values("valor", ascending=False))
                fig = px.bar(por_vendedor_abierto, x="vendedor", y="valor",
                             title="Valor esperado por vendedor (pipeline abierto)",
                             labels={"valor": "Valor esperado", "vendedor": ""})
                show_bar_labels(fig, kind="money")
                st.plotly_chart(fig, use_container_width=True, key="chart_22")
        with col_d:
            if won_crm.empty:
                st.info("No hay oportunidades ganadas con cierre en el período.")
            else:
                por_vendedor_ganado = (won_crm.groupby("vendedor", as_index=False)
                                       .agg(valor=("expected_revenue", "sum"))
                                       .sort_values("valor", ascending=False))
                fig = px.bar(por_vendedor_ganado, x="vendedor", y="valor",
                             title="Ganado por vendedor (fecha de cierre)",
                             labels={"valor": "COP", "vendedor": ""},
                             color_discrete_sequence=["#2ca02c"])
                show_bar_labels(fig, kind="money")
                st.plotly_chart(fig, use_container_width=True, key="chart_22b")

        with st.expander("📋 Detalle: creados en el período (fecha de creación)"):
            if leads.empty:
                st.info("Sin registros.")
            else:
                st.dataframe(
                    leads[["name", "tipo", "create_date", "vendedor", "equipo", "etapa",
                           "expected_revenue", "probability", "estado"]],
                    use_container_width=True, hide_index=True,
                )
        with st.expander("📋 Detalle: ganados del período (fecha de cierre)"):
            if won_crm.empty:
                st.info("Sin ganados.")
            else:
                st.dataframe(
                    won_crm[["name", "date_closed", "vendedor", "equipo", "cliente",
                             "expected_revenue"]],
                    use_container_width=True, hide_index=True,
                    column_config={
                        "date_closed": "Fecha de cierre",
                        "expected_revenue": st.column_config.NumberColumn(
                            "Valor ganado", format="$%,.0f"),
                    },
                )

        st.divider()
        st.markdown("#### ⏱️ Tiempo de ciclo: creación → cierre")
        st.caption("Días entre `create_date` y `date_closed` de oportunidades "
                   "**cerradas en el período** (ganadas y perdidas). No incluye abiertas.")
        if closed_crm.empty or "dias_ciclo" not in closed_crm.columns:
            st.info("No hay cierres con fechas de creación/cierre para medir el ciclo.")
        else:
            ciclo = closed_crm.dropna(subset=["dias_ciclo"]).copy()
            ciclo_gan = ciclo[ciclo["estado"] == "Ganada"]
            ciclo_per = ciclo[ciclo["estado"] == "Perdida"]

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Promedio general", f"{ciclo['dias_ciclo'].mean():.1f} días")
            m2.metric("Mediana general", f"{ciclo['dias_ciclo'].median():.1f} días")
            m3.metric("Promedio ganadas",
                      f"{ciclo_gan['dias_ciclo'].mean():.1f} días" if not ciclo_gan.empty else "—")
            m4.metric("Promedio perdidas",
                      f"{ciclo_per['dias_ciclo'].mean():.1f} días" if not ciclo_per.empty else "—")

            col_t1, col_t2 = st.columns(2)
            with col_t1:
                por_vend_ciclo = (ciclo.groupby("vendedor", as_index=False)
                                  .agg(dias_promedio=("dias_ciclo", "mean"),
                                       n=("name", "count"))
                                  .sort_values("dias_promedio", ascending=True))
                fig = px.bar(
                    por_vend_ciclo, x="dias_promedio", y="vendedor", orientation="h",
                    title="Días promedio creación→cierre por vendedor",
                    labels={"dias_promedio": "Días", "vendedor": ""},
                )
                show_bar_labels(fig, kind="hours")  # 1 decimal
                st.plotly_chart(fig, use_container_width=True, key="chart_ciclo_vendedor")
            with col_t2:
                fig = px.box(
                    ciclo, x="estado", y="dias_ciclo", color="estado",
                    title="Distribución de días de ciclo por resultado",
                    color_discrete_map={"Ganada": "#2ca02c", "Perdida": "#d62728"},
                    labels={"dias_ciclo": "Días", "estado": ""},
                )
                st.plotly_chart(fig, use_container_width=True, key="chart_ciclo_box")

            with st.expander("📋 Detalle de ciclo por oportunidad"):
                st.dataframe(
                    ciclo[["name", "create_date", "date_closed", "dias_ciclo",
                           "vendedor", "medio", "estado", "expected_revenue"]]
                    .sort_values("dias_ciclo", ascending=False),
                    use_container_width=True, hide_index=True,
                    column_config={
                        "name": "Oportunidad",
                        "create_date": "Creado",
                        "date_closed": "Cerrado",
                        "dias_ciclo": st.column_config.NumberColumn("Días ciclo", format="%.1f"),
                        "expected_revenue": st.column_config.NumberColumn(
                            "Valor", format="$%,.0f"),
                    },
                )

# ─────────────────────────────────────────────
# TAB: Leads y Conversión (todo el pipeline: leads + oportunidades)
# ─────────────────────────────────────────────
with tab_leads:
    if leads_full.empty and closed.empty:
        st.info("No hay leads, oportunidades ni cierres en el período seleccionado.")
    else:
        ganadas_f = closed[closed["estado"] == "Ganada"] if not closed.empty else closed
        perdidas_f = closed[closed["estado"] == "Perdida"] if not closed.empty else closed
        cerradas_f = len(ganadas_f) + len(perdidas_f)
        tasa_global = len(ganadas_f) / cerradas_f * 100 if cerradas_f else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Leads creados", len(leads_full))
        c2.metric("Ganadas (cerradas en el período)", len(ganadas_f))
        c3.metric("Perdidas (cerradas en el período)", len(perdidas_f))
        c4.metric("Tasa de conversión", f"{tasa_global:.1f}%")
        st.caption("Ganadas/Perdidas y la tasa de conversión se calculan por **fecha de "
                   "cierre**, no por fecha de creación: incluyen oportunidades que venías "
                   "trabajando desde antes del período y que se cerraron ahora. \"Leads "
                   "creados\" sí es estrictamente por fecha de creación, así que ambos "
                   "números no son necesariamente el mismo grupo de registros. Incluye "
                   "archivados.")

        if not leads_full.empty:
            # Regla de la dueña del proceso: tasa = ganadas (fecha de cierre) / leads creados.
            leads_x_vend = leads_full.groupby("vendedor", as_index=False).agg(leads=("name", "count"))
            ganadas_x_vend = (closed[closed["estado"] == "Ganada"]
                               .groupby("vendedor", as_index=False).agg(ganadas=("name", "count"))
                               if not closed.empty else pd.DataFrame(columns=["vendedor", "ganadas"]))
            conv_vendedor = leads_x_vend.merge(ganadas_x_vend, on="vendedor", how="left")
            conv_vendedor["ganadas"] = conv_vendedor["ganadas"].fillna(0).astype(int)
            conv_vendedor["leads"] = conv_vendedor["leads"].astype(int)
            conv_vendedor["tasa_conversion"] = conv_vendedor.apply(
                lambda r: r["ganadas"] / r["leads"] * 100 if r["leads"] else 0, axis=1)
            conv_vendedor = conv_vendedor.sort_values("leads", ascending=False)
            st.markdown("#### Leads y cierres por vendedor (números, no valores)")
            st.dataframe(
                conv_vendedor, use_container_width=True, hide_index=True,
                column_config={
                    "vendedor": "Vendedor",
                    "leads": "Leads creados",
                    "ganadas": "Cierres ganados",
                    "tasa_conversion": st.column_config.ProgressColumn(
                        "% conversión", format="%.1f%%", min_value=0, max_value=100),
                },
            )

            mensual_vendedor = (leads_full.groupby(["mes", "vendedor"], as_index=False)
                                .agg(leads=("name", "count")))
            fig = px.bar(mensual_vendedor, x="mes", y="leads", color="vendedor",
                         barmode="stack", title="Leads mensuales por vendedor (apilado)",
                         labels={"leads": "Leads", "mes": "Mes"})
            add_stack_totals(fig, mensual_vendedor, "mes", "leads", kind="count")
            st.plotly_chart(fig, use_container_width=True, key="chart_23")

            mensual_medio = (leads_full.groupby(["mes", "medio"], as_index=False)
                             .agg(leads=("name", "count")))
            fig = px.bar(mensual_medio, x="mes", y="leads", color="medio",
                         barmode="stack", title="Leads creados por medio y mes",
                         labels={"leads": "Leads", "mes": "Mes"})
            add_stack_totals(fig, mensual_medio, "mes", "leads", kind="count")
            st.plotly_chart(fig, use_container_width=True, key="chart_25")

        col_a, col_b = st.columns(2)
        with col_a:
            if not leads_full.empty:
                por_medio_ing = (leads_full.groupby("medio", as_index=False)
                                 .agg(ingresos=("expected_revenue", "sum"),
                                      leads=("name", "count"))
                                 .sort_values("ingresos", ascending=True))
                fig = px.bar(por_medio_ing, x="ingresos", y="medio", orientation="h",
                             title="Leads creados por medio (mayor a menor en ingresos)",
                             labels={"ingresos": "Ingresos esperados", "medio": ""})
                show_bar_labels(fig, kind="money")
                st.plotly_chart(fig, use_container_width=True, key="chart_26")
        with col_b:
            if not leads_full.empty:
                fig = px.bar(conv_vendedor.sort_values("tasa_conversion"),
                             x="tasa_conversion", y="vendedor", orientation="h",
                             title="Tasa de conversión por vendedor (ganadas / leads creados)",
                             text_auto=".1f",
                             labels={"tasa_conversion": "% conversión", "vendedor": ""})
                show_bar_labels(fig, kind="pct")
                st.plotly_chart(fig, use_container_width=True, key="chart_27")

        if not closed.empty:
            conv_medio = closed.groupby("medio", as_index=False).agg(
                ganadas=("estado", lambda s: (s == "Ganada").sum()),
                cerradas=("estado", "count"),
            )
            conv_medio["tasa_conversion"] = conv_medio.apply(
                lambda r: r["ganadas"] / r["cerradas"] * 100 if r["cerradas"] else 0, axis=1)
            conv_medio = conv_medio.sort_values("cerradas", ascending=False)
            st.markdown("#### Conversión por medio (por fecha de cierre)")
            st.caption("Ganadas / Cerradas con **date_closed** en el rango del sidebar.")

            col_cm1, col_cm2 = st.columns(2)
            with col_cm1:
                fig = px.bar(
                    conv_medio.sort_values("tasa_conversion", ascending=True),
                    x="tasa_conversion", y="medio", orientation="h",
                    title="% conversión por medio",
                    labels={"tasa_conversion": "% conversión", "medio": ""},
                )
                show_bar_labels(fig, kind="pct")
                st.plotly_chart(fig, use_container_width=True, key="chart_conv_medio_pct")
            with col_cm2:
                conv_largo = conv_medio.melt(
                    id_vars="medio", value_vars=["ganadas", "cerradas"],
                    var_name="concepto", value_name="cantidad")
                conv_largo["concepto"] = conv_largo["concepto"].map(
                    {"ganadas": "Ganadas", "cerradas": "Cerradas"})
                orden_medios = conv_medio["medio"].tolist()
                fig = px.bar(
                    conv_largo, x="medio", y="cantidad", color="concepto",
                    barmode="group", title="Ganadas vs. cerradas por medio",
                    category_orders={"medio": orden_medios},
                    color_discrete_map={"Ganadas": "#2ca02c", "Cerradas": "#9ca3af"},
                    labels={"cantidad": "Cantidad", "medio": "Medio"},
                )
                show_bar_labels(fig, kind="count")
                st.plotly_chart(fig, use_container_width=True, key="chart_conv_medio_vol")

            st.dataframe(
                conv_medio,
                use_container_width=True, hide_index=True,
                column_config={
                    "medio": "Medio",
                    "ganadas": "Ganadas",
                    "cerradas": "Cerradas",
                    "tasa_conversion": st.column_config.ProgressColumn(
                        "% conversión", format="%.1f%%", min_value=0, max_value=100),
                },
            )

        if not leads_full.empty:
            with st.expander("📋 Detalle de leads creados en el período"):
                st.dataframe(
                    leads_full[["name", "create_date", "vendedor", "equipo", "origen",
                                "medio", "estado", "expected_revenue"]],
                    use_container_width=True, hide_index=True,
                )
        if not closed.empty:
            with st.expander("📋 Detalle de cierres (ganados/perdidos) en el período"):
                st.dataframe(
                    closed[["name", "date_closed", "vendedor", "equipo", "medio",
                            "estado", "expected_revenue"]],
                    use_container_width=True, hide_index=True,
                )

# ─────────────────────────────────────────────
# TAB 4: Metas por vendedor (ganados CRM vs metas.csv)
# ─────────────────────────────────────────────
with tab_metas:
    metas = load_metas()
    won = load_won(d1, d2, team_ids)

    if metas.empty:
        st.warning("No se encontró `metas.csv` en el repositorio. Crea el archivo con "
                   "columnas `vendedor,meta_anual` en la raíz del repo.")
    elif won.empty:
        st.info("No hay oportunidades ganadas en el período seleccionado.")
    else:
        # Meta del período = meta anual prorrateada por los días del rango del sidebar
        dias_periodo = (date_to - date_from).days + 1

        ventas_vendedor = (won.groupby("vendedor", as_index=False)["expected_revenue"]
                           .sum().rename(columns={"expected_revenue": "vendido"}))

        # outer merge: muestra vendedores con meta sin ventas y viceversa
        cumpl = metas.merge(ventas_vendedor, on="vendedor", how="outer")
        cumpl["meta_anual"] = cumpl["meta_anual"].fillna(0)
        cumpl["vendido"] = cumpl["vendido"].fillna(0)
        cumpl["meta_periodo"] = cumpl["meta_anual"] * dias_periodo / 365.25
        cumpl["pct_periodo"] = cumpl.apply(
            lambda r: r["vendido"] / r["meta_periodo"] * 100 if r["meta_periodo"] else 0, axis=1)
        cumpl["pct_anual"] = cumpl.apply(
            lambda r: r["vendido"] / r["meta_anual"] * 100 if r["meta_anual"] else 0, axis=1)
        cumpl = cumpl.sort_values("pct_periodo", ascending=False)

        sin_meta = cumpl[(cumpl["meta_anual"] == 0) & (cumpl["vendido"] > 0)]["vendedor"].tolist()
        if sin_meta:
            st.warning("⚠️ Vendedores con ventas pero SIN meta en metas.csv (verifica que el "
                       f"nombre coincida exactamente con Odoo): {', '.join(sin_meta)}")

        con_meta = cumpl[cumpl["meta_anual"] > 0]

        c1, c2, c3, c4 = st.columns(4)
        total_meta = con_meta["meta_anual"].sum()
        total_meta_periodo = con_meta["meta_periodo"].sum()
        # "Vendido (ganados)" = TODO lo ganado por el equipo en el período (won, sin
        # duplicar), no solo lo de vendedores con meta — así coincide con el mismo
        # total en la pestaña "Metas por línea" en vez de sub-contarlo en silencio.
        total_vendido_equipo = won["expected_revenue"].sum()
        total_vendido_con_meta = con_meta["vendido"].sum()
        c1.metric("Meta anual (equipo)", fmt_money(total_meta))
        c2.metric(f"Meta del período ({dias_periodo} días)", fmt_money(total_meta_periodo))
        c3.metric("Vendido (ganados)", fmt_money(total_vendido_equipo))
        c4.metric("Cumplimiento del período",
                  f"{total_vendido_con_meta / total_meta_periodo * 100:.1f}%" if total_meta_periodo else "—",
                  help="El % de cumplimiento solo compara vendedores con meta definida "
                       "contra su propia meta; 'Vendido (ganados)' de arriba sí incluye "
                       "a todo el equipo, tenga meta o no.")

        # Tabla de cumplimiento
        tabla = con_meta[["vendedor", "meta_anual", "meta_periodo", "vendido",
                          "pct_periodo", "pct_anual"]].copy()
        st.dataframe(
            tabla,
            use_container_width=True, hide_index=True,
            column_config={
                "vendedor": "Vendedor",
                "meta_anual": st.column_config.NumberColumn("Meta anual", format="$%,.0f"),
                "meta_periodo": st.column_config.NumberColumn("Meta del período", format="$%,.0f"),
                "vendido": st.column_config.NumberColumn("Vendido", format="$%,.0f"),
                "pct_periodo": st.column_config.ProgressColumn(
                    "% del período", format="%.1f%%", min_value=0, max_value=150),
                "pct_anual": st.column_config.ProgressColumn(
                    "% anual", format="%.1f%%", min_value=0, max_value=100),
            },
        )

        col_a, col_b = st.columns(2)
        with col_a:
            comp = con_meta.melt(
                id_vars="vendedor",
                value_vars=["meta_periodo", "vendido"],
                var_name="concepto", value_name="valor")
            comp["concepto"] = comp["concepto"].map(
                {"meta_periodo": "Meta del período", "vendido": "Vendido"})
            fig = px.bar(comp, x="vendedor", y="valor", color="concepto",
                         barmode="group", title="Vendido vs. meta del período",
                         color_discrete_map={"Meta del período": "#9ca3af", "Vendido": "#1f77b4"},
                         labels={"valor": "COP", "vendedor": ""})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_28")
        with col_b:
            mensual = (won.groupby(["mes", "vendedor"], as_index=False)["expected_revenue"].sum())
            fig = px.bar(mensual, x="mes", y="expected_revenue", color="vendedor",
                         title="Ganados por mes y vendedor (reporte de ganados)",
                         labels={"expected_revenue": "COP", "mes": "Mes"})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_29")

        with st.expander("📋 Detalle de oportunidades ganadas"):
            st.dataframe(
                won[["name", "date_closed", "vendedor", "equipo", "expected_revenue"]],
                use_container_width=True, hide_index=True,
            )

    if not metas.empty:
        st.divider()
        st.markdown("#### 📅 Cumplimiento de meta x vendedor x mes")
        st.caption("Usa un año calendario completo (no el rango de fechas del sidebar), "
                  "para ver los 12 meses de una sola vez.")
        anio_metas_vend = st.selectbox("Año a analizar", options=range(hoy.year, hoy.year - 4, -1),
                                       index=0, key="anio_metas_vendedor")
        won_anio_vend = load_won(f"{anio_metas_vend}-01-01", f"{anio_metas_vend}-12-31", team_ids)
        if won_anio_vend.empty:
            st.info(f"No hay oportunidades ganadas en {anio_metas_vend}.")
        else:
            col_config_pivote_v = {"Vendedor": "Vendedor"}
            for m in MESES_ES:
                col_config_pivote_v[m] = st.column_config.NumberColumn(m, format="$%,.0f")
            col_config_pivote_v["Acumulado"] = st.column_config.NumberColumn("Acumulado", format="$%,.0f")
            col_config_pivote_v["% Cumpl. Anual"] = st.column_config.ProgressColumn(
                "% Cumpl. Anual", format="%.1f%%", min_value=0, max_value=150)
            pivote_vend = construir_pivote_vendedor(won_anio_vend, "expected_revenue",
                                                     anio_metas_vend, metas)
            st.dataframe(pivote_vend, use_container_width=True, hide_index=True,
                         column_config=col_config_pivote_v)

            with st.expander("📋 Detalle: cumplimiento MENSUAL por vendedor (no anual)"):
                st.caption("A diferencia de la tabla de arriba (que acumula todo el año contra "
                          "la meta anual completa), aquí cada mes se compara contra su propia "
                          "cuota mensual (meta anual / 12) — así se ve si un vendedor cumplió "
                          "SU mes, no solo si va bien en el acumulado del año.")
                meses_anio_vend = [f"{anio_metas_vend}-{m:02d}" for m in range(1, 13)]
                base_vend = pd.MultiIndex.from_product(
                    [metas["vendedor"], meses_anio_vend], names=["vendedor", "mes"]).to_frame(index=False)
                vendido_mensual_vend = (won_anio_vend.groupby(["vendedor", "mes"], as_index=False)
                                        ["expected_revenue"].sum().rename(columns={"expected_revenue": "vendido"}))
                detalle_vend = base_vend.merge(vendido_mensual_vend, on=["vendedor", "mes"], how="left")
                detalle_vend["vendido"] = detalle_vend["vendido"].fillna(0)
                meta_mensual_vend = metas.set_index("vendedor")["meta_anual"] / 12
                detalle_vend["meta_mensual"] = detalle_vend["vendedor"].map(meta_mensual_vend).fillna(0)
                detalle_vend["pct_cumpl_mensual"] = detalle_vend.apply(
                    lambda r: r["vendido"] / r["meta_mensual"] * 100 if r["meta_mensual"] else 0, axis=1)
                st.dataframe(
                    detalle_vend.sort_values(["vendedor", "mes"]),
                    use_container_width=True, hide_index=True,
                    column_config={
                        "vendedor": "Vendedor", "mes": "Mes",
                        "meta_mensual": st.column_config.NumberColumn("Meta mensual", format="$%,.0f"),
                        "vendido": st.column_config.NumberColumn("Vendido", format="$%,.0f"),
                        "pct_cumpl_mensual": st.column_config.ProgressColumn(
                            "% Cumpl. mensual", format="%.1f%%", min_value=0, max_value=150),
                    },
                )

# ─────────────────────────────────────────────
# TAB: Metas por línea (ganados CRM clasificados por etiqueta vs metas_lineas.csv)
# ─────────────────────────────────────────────
with tab_lineas:
    st.caption("Cumplimiento por línea con **ventas facturadas antes de impuestos** "
               "(Análisis de Facturas / `account.invoice.report`), no CRM. Las secciones "
               "de ganados CRM más abajo siguen usando oportunidades.")
    metas_lineas = load_metas_lineas()
    won_lineas = load_won_lineas(d1, d2, team_ids)

    # Facturado sin IVA por línea en el rango del sidebar (una pasada por categoría).
    partes_fact_l = []
    fact_mensual_parts = []
    for linea, categoria in LINEA_CATEGORIA.items():
        lf = load_invoice_lines_by_categoria(categoria)
        if lf.empty:
            continue
        mask = (lf["date"].dt.date >= date_from) & (lf["date"].dt.date <= date_to)
        lf = lf.loc[mask].copy()
        if lf.empty:
            continue
        partes_fact_l.append(pd.DataFrame({"linea": [linea], "vendido": [lf["monto"].sum()]}))
        res = lf.groupby("mes", as_index=False)["monto"].sum()
        res["linea"] = linea
        fact_mensual_parts.append(res)
    fact_mensual_l = (pd.concat(fact_mensual_parts, ignore_index=True)
                      if fact_mensual_parts else pd.DataFrame(columns=["mes", "monto", "linea"]))
    ventas_linea = (pd.concat(partes_fact_l, ignore_index=True)
                    if partes_fact_l else pd.DataFrame(columns=["linea", "vendido"]))

    if metas_lineas.empty:
        st.warning("No se encontró `data/metas_lineas.csv`. Crea el archivo con columnas "
                   "`linea,meta_anual` (el nombre de `linea` debe coincidir con "
                   "LINEA_CATEGORIA / etiquetas de negocio).")
    elif ventas_linea.empty and won_lineas.empty:
        st.info("No hay facturación ni oportunidades ganadas en el período seleccionado.")
    else:
        dias_periodo_l = (date_to - date_from).days + 1

        cumpl_l = metas_lineas.merge(ventas_linea, on="linea", how="outer")
        cumpl_l["meta_anual"] = cumpl_l["meta_anual"].fillna(0)
        cumpl_l["vendido"] = cumpl_l["vendido"].fillna(0)
        cumpl_l["meta_periodo"] = cumpl_l["meta_anual"] * dias_periodo_l / 365.25
        cumpl_l["pct_periodo"] = cumpl_l.apply(
            lambda r: r["vendido"] / r["meta_periodo"] * 100 if r["meta_periodo"] else 0, axis=1)
        cumpl_l["pct_anual"] = cumpl_l.apply(
            lambda r: r["vendido"] / r["meta_anual"] * 100 if r["meta_anual"] else 0, axis=1)
        cumpl_l = cumpl_l.sort_values("pct_periodo", ascending=False)

        sin_meta_l = cumpl_l[(cumpl_l["meta_anual"] == 0) & (cumpl_l["vendido"] > 0)]["linea"].tolist()
        if sin_meta_l:
            st.warning("⚠️ Líneas con ventas facturadas pero SIN meta en metas_lineas.csv: "
                       f"{', '.join(sin_meta_l)}")

        con_meta_l = cumpl_l[cumpl_l["meta_anual"] > 0]

        c1, c2, c3, c4 = st.columns(4)
        total_meta_l = con_meta_l["meta_anual"].sum()
        total_meta_periodo_l = con_meta_l["meta_periodo"].sum()
        total_vendido_equipo_l = ventas_linea["vendido"].sum() if not ventas_linea.empty else 0.0
        total_vendido_con_meta_l = con_meta_l["vendido"].sum()
        c1.metric("Meta anual (todas las líneas)", fmt_money(total_meta_l))
        c2.metric(f"Meta del período ({dias_periodo_l} días)", fmt_money(total_meta_periodo_l))
        c3.metric("Ventas facturadas (sin IVA)", fmt_money(total_vendido_equipo_l))
        c4.metric("Cumplimiento del período",
                  f"{total_vendido_con_meta_l / total_meta_periodo_l * 100:.1f}%" if total_meta_periodo_l else "—",
                  help="Compara lo facturado sin IVA de cada línea (categoría de producto) "
                       "contra su meta del período.")

        tabla_l = con_meta_l[["linea", "meta_anual", "meta_periodo", "vendido",
                              "pct_periodo", "pct_anual"]].copy()
        st.dataframe(
            tabla_l,
            use_container_width=True, hide_index=True,
            column_config={
                "linea": "Línea",
                "meta_anual": st.column_config.NumberColumn("Meta anual", format="$%,.0f"),
                "meta_periodo": st.column_config.NumberColumn("Meta del período", format="$%,.0f"),
                "vendido": st.column_config.NumberColumn("Facturado (sin IVA)", format="$%,.0f"),
                "pct_periodo": st.column_config.ProgressColumn(
                    "% del período", format="%.1f%%", min_value=0, max_value=150),
                "pct_anual": st.column_config.ProgressColumn(
                    "% anual", format="%.1f%%", min_value=0, max_value=100),
            },
        )

        col_a, col_b = st.columns(2)
        with col_a:
            comp_l = con_meta_l.melt(
                id_vars="linea",
                value_vars=["meta_periodo", "vendido"],
                var_name="concepto", value_name="valor")
            comp_l["concepto"] = comp_l["concepto"].map(
                {"meta_periodo": "Meta del período", "vendido": "Facturado (sin IVA)"})
            fig = px.bar(comp_l, x="linea", y="valor", color="concepto",
                         barmode="group", title="Facturado vs. meta del período por línea",
                         color_discrete_map={"Meta del período": "#9ca3af",
                                             "Facturado (sin IVA)": "#1f77b4"},
                         labels={"valor": "COP", "linea": ""})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_30")
        with col_b:
            if fact_mensual_l.empty:
                st.info("No hay facturación por línea en el período.")
            else:
                fig = px.bar(fact_mensual_l, x="mes", y="monto", color="linea",
                             title="Facturado por mes y línea (sin IVA)",
                             labels={"monto": "COP", "mes": "Mes"})
                show_bar_labels(fig, kind="money", text_size=14)
                st.plotly_chart(fig, use_container_width=True, key="chart_31")

        st.markdown("#### Ganado por mes y vendedor, por línea")
        for linea_nombre in con_meta_l["linea"].drop_duplicates():
            datos_linea = won_lineas[won_lineas["linea"] == linea_nombre]
            if datos_linea.empty:
                continue
            mensual_linea_vendedor = (datos_linea.groupby(["mes", "vendedor"], as_index=False)
                                      ["expected_revenue"].sum())
            fig = px.bar(mensual_linea_vendedor, x="mes", y="expected_revenue", color="vendedor",
                         barmode="group", title=f"{linea_nombre} — ganado por mes y vendedor",
                         labels={"expected_revenue": "COP", "mes": "Mes"})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(
                fig, use_container_width=True,
                key=f"chart_linea_vendedor_{linea_nombre}",
            )

        st.markdown("#### 🆕 Ganado por línea: cliente Nuevo vs. Recurrente")
        st.caption("Según la etiqueta de Odoo: la variación 'Nuevo' de la etiqueta de línea "
                  "(p. ej. 'Nuevo Implementación Odoo') marca que la venta ganada es de un "
                  "cliente nuevo; sin esa variación, se cuenta como recurrente.")
        won_lineas_clasif = won_lineas[won_lineas["linea"] != "Sin etiqueta"]
        sin_etiqueta_valor = won_lineas.loc[won_lineas["linea"] == "Sin etiqueta", "expected_revenue"].sum()
        if sin_etiqueta_valor:
            st.caption(f"⚠️ {fmt_money(sin_etiqueta_valor)} en oportunidades ganadas sin "
                      "etiqueta de línea reconocida — no se incluyen en este desglose.")
        if won_lineas_clasif.empty:
            st.info("No hay oportunidades ganadas con etiqueta de línea en el período.")
        else:
            nuevo_vs_rec = (won_lineas_clasif.groupby(["linea", "segmento"], as_index=False)
                            ["expected_revenue"].sum())
            fig = px.bar(nuevo_vs_rec, x="linea", y="expected_revenue", color="segmento",
                         barmode="stack", title="Ganado por línea: Nuevo vs. Recurrente",
                         color_discrete_map={"Nuevo": "#1f77b4", "Recurrente": "#9ca3af"},
                         labels={"expected_revenue": "COP", "linea": "Línea", "segmento": "Segmento"})
            add_stack_totals(fig, nuevo_vs_rec, "linea", "expected_revenue", kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_33")

            resumen_nuevo = (nuevo_vs_rec.pivot(index="linea", columns="segmento",
                                                 values="expected_revenue").fillna(0))
            for col in ("Nuevo", "Recurrente"):
                if col not in resumen_nuevo.columns:
                    resumen_nuevo[col] = 0.0
            resumen_nuevo["Total"] = resumen_nuevo["Nuevo"] + resumen_nuevo["Recurrente"]
            resumen_nuevo["% Nuevo"] = resumen_nuevo.apply(
                lambda r: r["Nuevo"] / r["Total"] * 100 if r["Total"] else 0, axis=1)
            resumen_nuevo = resumen_nuevo.reset_index()
            st.dataframe(
                resumen_nuevo, use_container_width=True, hide_index=True,
                column_config={
                    "linea": "Línea",
                    "Nuevo": st.column_config.NumberColumn("Nuevo", format="$%,.0f"),
                    "Recurrente": st.column_config.NumberColumn("Recurrente", format="$%,.0f"),
                    "Total": st.column_config.NumberColumn("Total", format="$%,.0f"),
                    "% Nuevo": st.column_config.ProgressColumn(
                        "% Nuevo", format="%.1f%%", min_value=0, max_value=100),
                },
            )

        with st.expander("📋 Detalle de oportunidades ganadas por línea"):
            st.dataframe(
                won_lineas[["name", "date_closed", "vendedor", "equipo", "linea",
                            "segmento", "expected_revenue"]],
                use_container_width=True, hide_index=True,
            )

# ─────────────────────────────────────────────
# TAB: Clientes Nuevos vs. Recurrentes
# ─────────────────────────────────────────────
with tab_clientes:
    all_invoices = load_all_invoices(team_ids)

    if all_invoices.empty:
        st.info("No hay facturas publicadas en el historial.")
    else:
        # Primera factura histórica de cada cliente (sin límite de fecha, para saber
        # si ya existía ANTES de que empezara el rango de fechas del sidebar)
        primera_factura = (all_invoices.groupby("cliente_id")["invoice_date"].min()
                           .rename("primera_factura"))

        mask_periodo = ((all_invoices["invoice_date"].dt.date >= date_from)
                        & (all_invoices["invoice_date"].dt.date <= date_to))
        fact_periodo = all_invoices[mask_periodo]
        if fact_periodo.empty:
            st.info("No hay facturas publicadas en el período seleccionado.")
        else:
            resumen = (fact_periodo.groupby(["cliente_id", "cliente"], as_index=False)
                      ["amount_untaxed_signed"].sum())
            resumen = resumen.merge(primera_factura, on="cliente_id", how="left")
            resumen["segmento"] = resumen["primera_factura"].apply(
                lambda d: "Nuevo" if d.date() >= date_from else "Recurrente")

            nuevos = resumen[resumen["segmento"] == "Nuevo"]
            recurrentes = resumen[resumen["segmento"] == "Recurrente"]

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Clientes facturados", len(resumen))
            c2.metric("Clientes nuevos", len(nuevos))
            c3.metric("Clientes recurrentes", len(recurrentes))
            c4.metric("% clientes nuevos",
                      f"{len(nuevos) / len(resumen) * 100:.1f}%" if len(resumen) else "—")
            st.caption("\"Nuevo\" = su primera factura registrada (en todo el historial) cae "
                      "dentro del rango de fechas seleccionado en el sidebar. \"Recurrente\" = "
                      "ya se le había facturado antes de que empezara ese rango.")

            col_a, col_b = st.columns(2)
            with col_a:
                conteo = resumen["segmento"].value_counts().reset_index()
                conteo.columns = ["segmento", "clientes"]
                fig = px.pie(conteo, names="segmento", values="clientes",
                            title="Clientes: nuevos vs. recurrentes", hole=0.45,
                            color="segmento",
                            color_discrete_map={"Nuevo": "#1f77b4", "Recurrente": "#9ca3af"})
                st.plotly_chart(fig, use_container_width=True, key="chart_34")
            with col_b:
                fact_seg = resumen.groupby("segmento", as_index=False)["amount_untaxed_signed"].sum()
                fig = px.pie(fact_seg, names="segmento", values="amount_untaxed_signed",
                            title="Facturación del período: nuevos vs. recurrentes", hole=0.45,
                            color="segmento",
                            color_discrete_map={"Nuevo": "#1f77b4", "Recurrente": "#9ca3af"})
                st.plotly_chart(fig, use_container_width=True, key="chart_35")

            st.markdown("#### Comparativo de facturación: nuevos vs. recurrentes")
            orden_segmento = {"Nuevo": 0, "Recurrente": 1}
            fact_seg_tabla = fact_seg.sort_values(by="segmento", key=lambda s: s.map(orden_segmento))
            total_fact = fact_seg_tabla["amount_untaxed_signed"].sum()
            fact_seg_tabla["pct_total"] = (
                fact_seg_tabla["amount_untaxed_signed"] / total_fact * 100 if total_fact else 0.0)
            fila_total = pd.DataFrame([{
                "segmento": "Total",
                "amount_untaxed_signed": total_fact,
                "pct_total": 100.0 if total_fact else 0.0,
            }])
            tabla_comp = pd.concat([fact_seg_tabla, fila_total], ignore_index=True)
            st.dataframe(
                tabla_comp,
                use_container_width=True, hide_index=True,
                column_config={
                    "segmento": "Segmento",
                    "amount_untaxed_signed": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                    "pct_total": st.column_config.NumberColumn("% del total", format="%.1f%%"),
                },
            )

            with st.expander(f"📋 Clientes NUEVOS en el período ({len(nuevos)})"):
                st.dataframe(
                    nuevos[["cliente", "amount_untaxed_signed"]]
                        .sort_values("amount_untaxed_signed", ascending=False),
                    use_container_width=True, hide_index=True,
                    column_config={
                        "cliente": "Cliente",
                        "amount_untaxed_signed": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                    },
                )
            with st.expander(f"📋 Clientes RECURRENTES en el período ({len(recurrentes)})"):
                recurrentes_tabla = recurrentes.copy()
                recurrentes_tabla["cliente_desde"] = recurrentes_tabla["primera_factura"].dt.date
                recurrentes_tabla["antiguedad_anios"] = (
                    (pd.Timestamp(date_to) - recurrentes_tabla["primera_factura"]).dt.days / 365.25
                ).round(1)
                st.dataframe(
                    recurrentes_tabla[["cliente", "cliente_desde", "antiguedad_anios",
                                       "amount_untaxed_signed"]]
                        .sort_values("amount_untaxed_signed", ascending=False),
                    use_container_width=True, hide_index=True,
                    column_config={
                        "cliente": "Cliente",
                        "cliente_desde": "Cliente desde",
                        "antiguedad_anios": "Antigüedad (años)",
                        "amount_untaxed_signed": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                    },
                )

    st.divider()
    st.markdown("#### 🏆 Top 10 clientes del año: facturado y CRM ganados")
    st.caption("Facturado en COP sin IVA (`account.invoice.report`). CRM = oportunidades ganadas.")
    anio_top_clientes = st.selectbox("Año a analizar", options=range(hoy.year, hoy.year - 4, -1),
                                     index=0, key="anio_top_clientes")
    fact_anio_cli = load_invoice_report(
        f"{anio_top_clientes}-01-01", f"{anio_top_clientes}-12-31", team_ids)
    won_anio = load_won(f"{anio_top_clientes}-01-01", f"{anio_top_clientes}-12-31", team_ids)

    col_t1, col_t2 = st.columns(2)
    with col_t1:
        if fact_anio_cli.empty:
            st.info(f"No hay facturación en {anio_top_clientes}.")
        else:
            top_ventas_anio = (fact_anio_cli.groupby("cliente", as_index=False)["monto"]
                               .sum().sort_values("monto", ascending=True).tail(10))
            fig = px.bar(top_ventas_anio, x="monto", y="cliente", orientation="h",
                         title=f"Top 10 clientes por facturado — {anio_top_clientes}", text_auto=".2s",
                         labels={"monto": "COP sin IVA", "cliente": ""})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_36")
            st.dataframe(
                top_ventas_anio.sort_values("monto", ascending=False),
                use_container_width=True, hide_index=True,
                column_config={
                    "cliente": "Cliente",
                    "monto": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                },
            )
    with col_t2:
        if won_anio.empty:
            st.info(f"No hay oportunidades ganadas en {anio_top_clientes}.")
        else:
            top_oport_anio = (won_anio.groupby("cliente", as_index=False)
                              .agg(ganados=("name", "count"), valor=("expected_revenue", "sum"))
                              .sort_values("ganados", ascending=True).tail(10))
            fig = px.bar(top_oport_anio, x="ganados", y="cliente", orientation="h",
                         title=f"Top 10 clientes por CRM ganados — {anio_top_clientes}",
                         text_auto=True, labels={"ganados": "Oportunidades", "cliente": ""})
            show_bar_labels(fig, kind="count")
            st.plotly_chart(fig, use_container_width=True, key="chart_37")
            st.dataframe(
                top_oport_anio.sort_values("ganados", ascending=False),
                use_container_width=True, hide_index=True,
                column_config={
                    "cliente": "Cliente",
                    "ganados": "Ganados",
                    "valor": st.column_config.NumberColumn("Valor CRM", format="$%,.0f"),
                },
            )

# ─────────────────────────────────────────────
# TAB: Soporte y Mantenimiento (categoría de producto "Soporte")
# ─────────────────────────────────────────────
with tab_soporte:
    st.caption("Misma fuente que INFORME SOPORTE: `price_subtotal` sin IVA, categoría "
               "**Soporte** exacta, solo **factura de cliente**, estado ≠ borrador/cancelado, "
               "**sin movimiento de reversión**. Recarga con **R** si ves caché vieja.")
    soporte_lines = load_invoice_lines_by_categoria(LINEA_CATEGORIA["Soporte y Mantenimiento"])

    if soporte_lines.empty:
        st.info("No hay facturación de la categoría 'Soporte' en el historial.")
    else:
        anio_ref = hoy.year
        sop_anio = soporte_lines[soporte_lines["date"].dt.year == anio_ref]
        total_anio_sop = float(sop_anio["monto"].sum()) if not sop_anio.empty else 0.0
        # Referencia INFORME 2026 (Análisis de Facturas con el filtro personalizado).
        ref_soporte_2026 = 193_145_600.00
        delta = total_anio_sop - ref_soporte_2026 if anio_ref == 2026 else None
        st.metric(
            f"Soporte facturado {anio_ref} (sin IVA)",
            fmt_money(total_anio_sop),
            delta=(f"{delta:+,.0f} vs INFORME" if delta is not None else None),
            help="Debe coincidir con Análisis de Facturas → categoría Soporte "
                 "(193.145.600 en 2026 con el filtro del INFORME).",
        )
        if anio_ref == 2026 and not sop_anio.empty:
            mens_ref = {
                "2026-01": 14_554_400.0, "2026-02": 8_210_800.0, "2026-03": 18_344_000.0,
                "2026-04": 16_460_800.0, "2026-05": 12_160_800.0, "2026-06": 9_160_000.0,
                "2026-07": 29_261_600.0, "2026-08": 21_510_800.0, "2026-09": 22_360_800.0,
                "2026-10": 20_560_800.0, "2026-11": 20_560_800.0,
            }
            mens_dash = sop_anio.groupby("mes")["monto"].sum().to_dict()
            filas_cmp = []
            for mes, ref in mens_ref.items():
                val = float(mens_dash.get(mes, 0.0))
                filas_cmp.append({
                    "Mes": mes, "Dashboard": val, "INFORME": ref,
                    "Diff": val - ref,
                })
            cmp_df = pd.DataFrame(filas_cmp)
            with st.expander("🔎 Soporte 2026: dashboard vs INFORME (por mes)", expanded=abs(delta or 0) > 1):
                st.dataframe(
                    cmp_df, use_container_width=True, hide_index=True,
                    column_config={
                        "Dashboard": st.column_config.NumberColumn(format="$%,.2f"),
                        "INFORME": st.column_config.NumberColumn(format="$%,.2f"),
                        "Diff": st.column_config.NumberColumn(format="$%,.2f"),
                    },
                )
        # Primera vez que se facturó Soporte a cada cliente (sin límite de fecha)
        primera_soporte = (soporte_lines.groupby("cliente_id")["date"].min()
                           .rename("primera_factura_soporte"))

        mask_periodo = ((soporte_lines["date"].dt.date >= date_from)
                        & (soporte_lines["date"].dt.date <= date_to))
        soporte_periodo = soporte_lines[mask_periodo].copy()

        if soporte_periodo.empty:
            st.info("No hay facturación de Soporte en el período seleccionado.")
        else:
            soporte_periodo = soporte_periodo.merge(primera_soporte, on="cliente_id", how="left")
            soporte_periodo["segmento"] = soporte_periodo["primera_factura_soporte"].apply(
                lambda d: "Nuevo" if d.date() >= date_from else "Ya se facturaba antes")

            total_periodo = soporte_periodo["monto"].sum()
            nuevo_monto = soporte_periodo.loc[soporte_periodo["segmento"] == "Nuevo", "monto"].sum()
            recurrente_monto = total_periodo - nuevo_monto

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Facturado Soporte (período)", fmt_money(total_periodo))
            c2.metric("De clientes nuevos", fmt_money(nuevo_monto))
            c3.metric("De clientes que ya se facturaban", fmt_money(recurrente_monto))
            c4.metric("% nuevo", f"{nuevo_monto / total_periodo * 100:.1f}%" if total_periodo else "—")
            st.caption("\"Nuevo\" = la primera factura de Soporte de ese cliente (en todo el "
                      "historial) cae dentro del rango de fechas del sidebar. \"Ya se facturaba "
                      "antes\" = ya se le venía facturando Soporte desde antes de que empezara "
                      "ese rango.")

            mensual_seg = (soporte_periodo.groupby(["mes", "segmento"], as_index=False)["monto"].sum())
            fig = px.bar(mensual_seg, x="mes", y="monto", color="segmento", barmode="stack",
                         title="Facturación de Soporte por mes: nuevo vs. ya se facturaba",
                         color_discrete_map={"Nuevo": "#1f77b4", "Ya se facturaba antes": "#9ca3af"},
                         labels={"monto": "Facturado", "mes": "Mes"})
            add_stack_totals(fig, mensual_seg, "mes", "monto", kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_38")

            st.markdown("#### Comparativo de facturación de Soporte")
            resumen_seg = soporte_periodo.groupby("segmento", as_index=False)["monto"].sum()
            orden_seg = {"Nuevo": 0, "Ya se facturaba antes": 1}
            resumen_seg = resumen_seg.sort_values(by="segmento", key=lambda s: s.map(orden_seg))
            resumen_seg["pct_total"] = (
                resumen_seg["monto"] / total_periodo * 100 if total_periodo else 0.0)
            fila_total = pd.DataFrame([{
                "segmento": "Total", "monto": total_periodo,
                "pct_total": 100.0 if total_periodo else 0.0,
            }])
            tabla_soporte = pd.concat([resumen_seg, fila_total], ignore_index=True)
            st.dataframe(
                tabla_soporte, use_container_width=True, hide_index=True,
                column_config={
                    "segmento": "Segmento",
                    "monto": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                    "pct_total": st.column_config.NumberColumn("% del total", format="%.1f%%"),
                },
            )

            por_cliente = (soporte_periodo.groupby(["cliente", "segmento"], as_index=False)["monto"]
                          .sum().sort_values("monto", ascending=False))
            with st.expander("📋 Detalle por cliente"):
                st.dataframe(
                    por_cliente, use_container_width=True, hide_index=True,
                    column_config={
                        "cliente": "Cliente",
                        "segmento": "Segmento",
                        "monto": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                    },
                )

# ─────────────────────────────────────────────
# TAB: Rentabilidad (facturado vs. costos del equipo, por línea)
# Oculta temporalmente: volver a incluir en st.tabs y cambiar if False → with tab_rentabilidad
# ─────────────────────────────────────────────
if False:
    st.caption("Rentabilidad = Facturado − Costos del equipo, por línea de negocio. Usa un "
               "año calendario completo (no el rango de fechas del sidebar). Los costos salen "
               "de la contabilidad analítica de Odoo: una cuenta analítica por línea, tomando "
               "los movimientos de importe negativo (convención estándar de Odoo para egresos).")

    anio_rent = st.selectbox("Año a analizar", options=range(hoy.year, hoy.year - 4, -1),
                             index=0, key="anio_rentabilidad")
    datos_rent = load_cumplimiento_mensual(anio_rent, team_ids)
    costos_rent, err_rent = load_analytic_costs_by_linea(f"{anio_rent}-01-01", f"{anio_rent}-12-31")

    if err_rent:
        st.warning(err_rent)
    else:
        facturado_linea = datos_rent.groupby("linea", as_index=False)["facturado"].sum()
        if costos_rent.empty:
            costo_linea = pd.DataFrame({"linea": list(LINEA_CATEGORIA.keys()), "costo": 0.0})
        else:
            costo_linea = costos_rent.groupby("linea", as_index=False)["costo"].sum()

        rent_tabla = facturado_linea.merge(costo_linea, on="linea", how="left")
        rent_tabla["costo"] = rent_tabla["costo"].fillna(0)
        rent_tabla["rentabilidad"] = rent_tabla["facturado"] - rent_tabla["costo"]
        rent_tabla["margen_pct"] = rent_tabla.apply(
            lambda r: r["rentabilidad"] / r["facturado"] * 100 if r["facturado"] else 0, axis=1)

        if costos_rent.empty:
            st.info("No se encontraron movimientos de costo en las cuentas analíticas de las "
                   "líneas para este año — revisa que existan las cuentas analíticas "
                   f"({', '.join(LINEA_CUENTA_ANALITICA.values())}) y que tengan movimientos "
                   "con importe negativo en el período.")

        total_fact = rent_tabla["facturado"].sum()
        total_costo = rent_tabla["costo"].sum()
        total_rent = total_fact - total_costo
        margen_total = (total_rent / total_fact * 100) if total_fact else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Facturado total", fmt_money(total_fact))
        c2.metric("Costo total del equipo", fmt_money(total_costo))
        c3.metric("Rentabilidad", fmt_money(total_rent))
        c4.metric("Margen", f"{margen_total:.1f}%")

        st.dataframe(
            rent_tabla, use_container_width=True, hide_index=True,
            column_config={
                "linea": "Línea",
                "facturado": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                "costo": st.column_config.NumberColumn("Costo del equipo", format="$%,.0f"),
                "rentabilidad": st.column_config.NumberColumn("Rentabilidad", format="$%,.0f"),
                "margen_pct": st.column_config.NumberColumn("Margen", format="%.1f%%"),
            },
        )

        fig = px.bar(rent_tabla, x="linea", y=["facturado", "costo"], barmode="group",
                     title=f"Facturado vs. costo del equipo por línea — {anio_rent}",
                     labels={"value": "COP", "linea": "Línea", "variable": "Concepto"})
        show_bar_labels(fig, kind="money")
        st.plotly_chart(fig, use_container_width=True, key="chart_39")

        if not costos_rent.empty:
            costo_mensual = costos_rent.groupby(["mes", "linea"], as_index=False)["costo"].sum()
            fact_mensual = datos_rent[["mes", "linea", "facturado"]]
            rent_mensual = fact_mensual.merge(costo_mensual, on=["mes", "linea"], how="left")
            rent_mensual["costo"] = rent_mensual["costo"].fillna(0)
            rent_mensual["rentabilidad"] = rent_mensual["facturado"] - rent_mensual["costo"]

            fig = px.bar(rent_mensual, x="mes", y="rentabilidad", color="linea", barmode="group",
                         title="Rentabilidad mensual por línea",
                         labels={"rentabilidad": "COP", "mes": "Mes", "linea": "Línea"})
            show_bar_labels(fig, kind="money")
            st.plotly_chart(fig, use_container_width=True, key="chart_40")

            with st.expander("📋 Detalle mensual de rentabilidad por línea"):
                st.dataframe(
                    rent_mensual, use_container_width=True, hide_index=True,
                    column_config={
                        "mes": "Mes", "linea": "Línea",
                        "facturado": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                        "costo": st.column_config.NumberColumn("Costo", format="$%,.0f"),
                        "rentabilidad": st.column_config.NumberColumn("Rentabilidad", format="$%,.0f"),
                    },
                )

# ─────────────────────────────────────────────
# TAB: Horas y Actividad del equipo
# ─────────────────────────────────────────────
with tab_equipo:
    roster_td = load_td_vendedores(team_ids)

    st.markdown("#### ⏱️ Horas vendidas mes a mes")
    st.caption("Fuente EXCLUSIVA: líneas de Órdenes de Venta del equipo de Transformación "
               "Digital (sale.order.line, ya acotadas al equipo TD del sidebar). Usa el "
               "campo `horas` (módulo propio `custom_add_hours`, calculado desde los "
               "atributos Complejidad/Tamaño del producto — así se venden los proyectos de "
               "Implementación) cuando está poblado; si no, cae a `product_uom_qty` de "
               "líneas cuya UoM se llama 'Horas' (venta simple por horas). No se mezclan "
               "horas de otras fuentes (parte de horas, actividades). Rango de fechas del "
               "sidebar. El vendedor se restringe además al roster oficial de TD.")
    horas_lineas, err_horas = load_sold_hours_lines(d1, d2, team_ids)
    if err_horas:
        st.warning(err_horas)
    elif horas_lineas.empty:
        st.info("No hay líneas vendidas en horas en el período seleccionado.")
    else:
        horas_df = horas_lineas.merge(
            sales[["id", "date_order", "vendedor", "equipo"]],
            left_on="order_id_num", right_on="id", how="inner")
        horas_df["mes"] = horas_df["date_order"].dt.to_period("M").astype(str)
        if not roster_td.empty:
            horas_df = horas_df[horas_df["vendedor"].isin(roster_td["vendedor"])]

        c1, c2 = st.columns(2)
        c1.metric("Horas vendidas (período)", f"{horas_df['horas_vendidas'].sum():,.1f} h")
        c2.metric("Líneas", len(horas_df))

        col_h1, col_h2 = st.columns(2)
        with col_h1:
            mensual_horas = horas_df.groupby("mes", as_index=False)["horas_vendidas"].sum()
            fig = px.bar(mensual_horas, x="mes", y="horas_vendidas", text_auto=".1f",
                         title="Horas vendidas por mes",
                         labels={"horas_vendidas": "Horas", "mes": "Mes"})
            show_bar_labels(fig, kind="hours")
            st.plotly_chart(fig, use_container_width=True, key="chart_41")
        with col_h2:
            mensual_horas_vendedor = (horas_df.groupby(["mes", "vendedor"], as_index=False)
                                      ["horas_vendidas"].sum())
            fig = px.bar(mensual_horas_vendedor, x="mes", y="horas_vendidas", color="vendedor",
                         barmode="group", title="Horas vendidas por vendedor y mes",
                         labels={"horas_vendidas": "Horas", "mes": "Mes"})
            show_bar_labels(fig, kind="hours")
            st.plotly_chart(fig, use_container_width=True, key="chart_42")

    st.divider()
    st.markdown("#### 📋 Tabla gerencial: actividades comerciales y oportunidades cerradas — TD")
    st.caption(
        "Dos fuentes independientes, sin mezclar: (1) **Actividades** = parte de horas del "
        "proyecto **Gestión comercial** — cada columna es el nombre REAL de la tarea "
        "(GC3, GC5, GC6, GC8 y las demás que existan). (2) **Oportunidades cerradas** = "
        "oportunidades GANADAS (crm.lead.won_status == 'won', fecha de cierre en el rango). "
        "Ambas se cruzan contra el roster oficial de vendedores de TD "
        "(crm.team.member_ids). Rango de fechas del sidebar."
    )

    actividades_gc14, err_gc14 = load_actividades_gc14(d1, d2, team_ids)
    won_periodo_equipo = load_won(d1, d2, team_ids)
    if not roster_td.empty and not won_periodo_equipo.empty:
        won_periodo_equipo = won_periodo_equipo[won_periodo_equipo["vendedor"].isin(roster_td["vendedor"])]

    if roster_td.empty:
        st.warning("No se encontró el roster de vendedores de TD (crm.team.member_ids) — no "
                  "se puede armar la tabla sin saber quiénes son los vendedores de TD.")
    elif err_gc14:
        st.warning(err_gc14)
    else:
        cerradas_vend = (
            won_periodo_equipo.groupby("vendedor", as_index=False)
            .agg(**{"Oportunidades cerradas": ("name", "count")})
            if not won_periodo_equipo.empty
            else pd.DataFrame(columns=["vendedor", "Oportunidades cerradas"])
        )

        if not actividades_gc14.empty:
            pivote_act = (actividades_gc14.groupby(["vendedor", "tarea"], as_index=False)
                          .agg(cantidad=("unit_amount", "count")))
            tabla_act = pivote_act.pivot(index="vendedor", columns="tarea", values="cantidad")
        else:
            tabla_act = pd.DataFrame(index=pd.Index([], name="vendedor"))
            st.info("No hay partes de horas en el proyecto Gestión comercial para "
                   "vendedores de TD en el período seleccionado — la tabla solo mostrará "
                   "oportunidades cerradas.")

        tabla_final = roster_td[["vendedor"]].merge(
            tabla_act.reset_index(), on="vendedor", how="left")
        tabla_final = tabla_final.merge(cerradas_vend, on="vendedor", how="left")
        cols_numericas = [c for c in tabla_final.columns if c != "vendedor"]
        tabla_final[cols_numericas] = tabla_final[cols_numericas].fillna(0).astype(int)
        tabla_final = tabla_final.rename(columns={"vendedor": "Vendedor"})

        st.dataframe(tabla_final, use_container_width=True, hide_index=True)

        if not actividades_gc14.empty:
            with st.expander("📋 Detalle de partes de horas — proyecto Gestión comercial"):
                st.dataframe(
                    actividades_gc14[["date", "vendedor", "tarea", "unit_amount", "name"]]
                        .sort_values("date", ascending=False),
                    use_container_width=True, hide_index=True,
                    column_config={
                        "date": "Fecha", "vendedor": "Vendedor", "tarea": "Tarea (actividad)",
                        "unit_amount": st.column_config.NumberColumn("Horas", format="%.1f"),
                        "name": "Descripción",
                    },
                )
        if not won_periodo_equipo.empty:
            with st.expander("📋 Detalle de oportunidades cerradas (ganadas)"):
                st.dataframe(
                    won_periodo_equipo[["name", "date_closed", "vendedor", "equipo", "expected_revenue"]],
                    use_container_width=True, hide_index=True,
                )

    st.divider()
    st.markdown("#### 👥 Actividades del equipo — tabla mes a mes (últimos 12 meses)")
    st.caption("Fuente: `crm.activity.report` (CRM → Informes → Actividades), agrupado por "
              "`mail_activity_type_id`. Solo etiquetas **Soporte** e **Implementación Odoo**. "
              "Ventana fija de 12 meses (no el sidebar). Mide presentación de negocio, "
              "propuesta, socialización/sustentación y seguimiento.")

    desde_12m_gc = (pd.Period(hoy, freq="M") - 11).to_timestamp().date()
    meses_12 = meses_ultimos_n(hoy, 12)
    actividades_crm, err_act_crm = load_crm_activity_report(
        desde_12m_gc.isoformat(), hoy.isoformat(), team_ids)
    if err_act_crm:
        st.warning(err_act_crm)
    elif actividades_crm.empty:
        st.info("No hay actividades de Implementación Odoo o Soporte y Mantenimiento en "
                "crm.activity.report para los últimos 12 meses.")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Actividades registradas", len(actividades_crm))
        c2.metric("De los 4 tipos del brief",
                  int((actividades_crm["tipo_brief"].isin(TIPOS_ACTIVIDAD)).sum()))
        otras_n = int((actividades_crm["tipo_brief"] == "Otras").sum())
        c3.metric("Otras (no mapeadas)", otras_n)
        if otras_n:
            nombres_otras = sorted(
                actividades_crm.loc[actividades_crm["tipo_brief"] == "Otras", "tipo"].unique())
            st.caption("Tipos de Odoo no mapeados a los 4 del brief: " + ", ".join(nombres_otras))

        col_cfg_act = {
            "mes": "Mes",
            "linea": "Línea",
            "vendedor": "Vendedor",
            "Total": st.column_config.NumberColumn("Total", format="%d"),
        }
        for tipo in TIPOS_ACTIVIDAD:
            col_cfg_act[tipo] = st.column_config.NumberColumn(tipo, format="%d")

        st.markdown("##### Por línea (Soporte e Implementación Odoo)")
        tabla_linea = tabla_actividades_mes_a_mes(
            actividades_crm, "linea", meses_12, extras=LINEAS_ACTIVIDAD)
        st.dataframe(tabla_linea, use_container_width=True, hide_index=True,
                     column_config=col_cfg_act)

        st.markdown("##### Por vendedor")
        act_unicas = (actividades_crm.drop_duplicates("id")
                      if "id" in actividades_crm.columns else actividades_crm)
        tabla_vend = tabla_actividades_mes_a_mes(act_unicas, "vendedor", meses_12)
        st.dataframe(tabla_vend, use_container_width=True, hide_index=True,
                     column_config=col_cfg_act)

        col_a1, col_a2 = st.columns(2)
        with col_a1:
            cantidad_linea_mes = (actividades_crm.groupby(["mes", "linea"], as_index=False)
                                  .agg(cantidad=("tipo", "count")))
            fig = px.bar(cantidad_linea_mes, x="mes", y="cantidad", color="linea", barmode="group",
                         title="Actividades por línea y mes",
                         labels={"cantidad": "Actividades", "mes": "Mes", "linea": "Línea"})
            show_bar_labels(fig, kind="count")
            st.plotly_chart(fig, use_container_width=True, key="chart_43")
        with col_a2:
            act_brief_g = actividades_crm[actividades_crm["tipo_brief"].isin(TIPOS_ACTIVIDAD)]
            if not act_brief_g.empty:
                por_tipo_vend = (act_brief_g.groupby(["tipo_brief", "vendedor"], as_index=False)
                                 .agg(cantidad=("mes", "count")))
                fig = px.bar(por_tipo_vend, x="tipo_brief", y="cantidad", color="vendedor",
                             barmode="group", title="Actividades por tipo y vendedor",
                             labels={"cantidad": "Actividades", "tipo_brief": "Tipo"})
                show_bar_labels(fig, kind="count")
                st.plotly_chart(fig, use_container_width=True, key="chart_44")

        with st.expander("📋 Detalle de actividades"):
            st.dataframe(
                actividades_crm[["date", "linea", "tipo", "tipo_brief", "vendedor", "equipo"]]
                    .sort_values("date", ascending=False),
                use_container_width=True, hide_index=True,
                column_config={
                    "date": "Fecha", "linea": "Línea", "tipo": "Tipo Odoo",
                    "tipo_brief": "Tipo brief", "vendedor": "Vendedor", "equipo": "Equipo",
                },
            )

    st.markdown("##### Horas comerciales mes a mes (GC3 / GC5 / GC6 / GC8)")
    st.caption("Tareas del proyecto **Gestión comercial** (no son proyectos aparte): "
               "GC3 presentación de negocio, GC5 cotización/propuesta, GC6 sustentación, "
               "GC8 seguimiento. Últimos 12 meses. El parte de horas no trae etiqueta CRM.")
    horas_gc14, err_horas_gc = load_actividades_gc14(
        desde_12m_gc.isoformat(), hoy.isoformat(), team_ids)
    if err_horas_gc:
        st.warning(err_horas_gc)
    elif horas_gc14.empty:
        st.info("No hay partes de horas en el proyecto Gestión comercial para los últimos 12 meses.")
    else:
        horas_gc = horas_gc14.copy()
        horas_gc["mes"] = pd.to_datetime(horas_gc["date"]).dt.to_period("M").astype(str)
        horas_mapeadas = horas_gc[horas_gc["tipo_brief"].isin(TIPOS_ACTIVIDAD)]
        if horas_mapeadas.empty:
            tareas = sorted(horas_gc["tarea"].dropna().unique().tolist())
            st.info("No se encontraron tareas GC3/GC5/GC6/GC8. Tareas vistas: "
                    + (", ".join(tareas) if tareas else "ninguna") + ".")
        else:
            horas_sum = (horas_mapeadas.groupby(["mes", "tipo_brief"], as_index=False)
                         ["unit_amount"].sum())
            tabla_h = horas_sum.pivot_table(
                index="mes", columns="tipo_brief", values="unit_amount",
                aggfunc="sum", fill_value=0)
            for tipo in TIPOS_ACTIVIDAD:
                if tipo not in tabla_h.columns:
                    tabla_h[tipo] = 0.0
            tabla_h = tabla_h.reindex(meses_12, fill_value=0)
            tabla_h = tabla_h[TIPOS_ACTIVIDAD]
            tabla_h["Total"] = tabla_h[TIPOS_ACTIVIDAD].sum(axis=1)
            tabla_h = tabla_h.reset_index().rename(columns={"index": "mes"})
            col_cfg_h = {"mes": "Mes"}
            for tipo in TIPOS_ACTIVIDAD + ["Total"]:
                col_cfg_h[tipo] = st.column_config.NumberColumn(tipo, format="%.1f")
            st.dataframe(tabla_h, use_container_width=True, hide_index=True,
                         column_config=col_cfg_h)

            fig = px.bar(horas_sum, x="mes", y="unit_amount", color="tipo_brief",
                         barmode="group", title="Horas por tipo GC y mes",
                         labels={"unit_amount": "Horas", "mes": "Mes", "tipo_brief": "Tipo"})
            show_bar_labels(fig, kind="hours")
            st.plotly_chart(fig, use_container_width=True, key="chart_45")

            st.markdown("##### Validación de horas por vendedor")
            st.caption("Mismas tareas GC3/GC5/GC6/GC8 y los mismos 12 meses. El vendedor "
                       "sale del empleado que registró el parte de horas, cruzado con el "
                       "roster TD. Sirve para contrastar quién cargó tiempo y en qué tipo.")
            vendedores_h = sorted(horas_mapeadas["vendedor"].dropna().unique().tolist())
            seleccion_h = st.multiselect(
                "Filtrar vendedores",
                options=vendedores_h,
                default=vendedores_h,
                key="filtro_horas_vendedores",
            )
            horas_f = (horas_mapeadas[horas_mapeadas["vendedor"].isin(seleccion_h)]
                       if seleccion_h else horas_mapeadas.iloc[0:0])

            col_cfg_hv = {"vendedor": "Vendedor", "mes": "Mes"}
            for tipo in TIPOS_ACTIVIDAD + ["Total"]:
                col_cfg_hv[tipo] = st.column_config.NumberColumn(tipo, format="%.1f")

            if horas_f.empty:
                st.info("Selecciona al menos un vendedor para ver las horas.")
            else:
                tot_v = horas_f.pivot_table(
                    index="vendedor", columns="tipo_brief", values="unit_amount",
                    aggfunc="sum", fill_value=0)
                for tipo in TIPOS_ACTIVIDAD:
                    if tipo not in tot_v.columns:
                        tot_v[tipo] = 0.0
                tot_v = tot_v[TIPOS_ACTIVIDAD]
                tot_v["Total"] = tot_v[TIPOS_ACTIVIDAD].sum(axis=1)
                tot_v = tot_v.sort_values("Total", ascending=False).reset_index()
                st.markdown("**Acumulado 12 meses por vendedor**")
                st.dataframe(tot_v, use_container_width=True, hide_index=True,
                             column_config=col_cfg_hv)

                mes_vend = horas_f.pivot_table(
                    index=["mes", "vendedor"], columns="tipo_brief",
                    values="unit_amount", aggfunc="sum", fill_value=0)
                for tipo in TIPOS_ACTIVIDAD:
                    if tipo not in mes_vend.columns:
                        mes_vend[tipo] = 0.0
                idx_mv = pd.MultiIndex.from_product(
                    [meses_12, seleccion_h or vendedores_h], names=["mes", "vendedor"])
                mes_vend = mes_vend.reindex(idx_mv, fill_value=0)
                mes_vend = mes_vend[TIPOS_ACTIVIDAD]
                mes_vend["Total"] = mes_vend[TIPOS_ACTIVIDAD].sum(axis=1)
                mes_vend = mes_vend.reset_index()
                st.markdown("**Mes a mes por vendedor**")
                st.dataframe(mes_vend, use_container_width=True, hide_index=True,
                             column_config=col_cfg_hv)

                horas_mes_vend = (horas_f.groupby(["mes", "vendedor"], as_index=False)
                                  ["unit_amount"].sum())
                fig = px.bar(horas_mes_vend, x="mes", y="unit_amount", color="vendedor",
                             barmode="stack", title="Horas comerciales por vendedor y mes",
                             labels={"unit_amount": "Horas", "mes": "Mes"})
                add_stack_totals(fig, horas_mes_vend, "mes", "unit_amount", kind="hours")
                st.plotly_chart(fig, use_container_width=True, key="chart_horas_vendedor")

                with st.expander("📋 Detalle de partes de horas (para validar línea a línea)"):
                    detalle_h = (horas_f[["date", "vendedor", "tarea", "tipo_brief",
                                          "unit_amount", "name"]]
                                 .sort_values(["vendedor", "date"], ascending=[True, False]))
                    st.dataframe(
                        detalle_h, use_container_width=True, hide_index=True,
                        column_config={
                            "date": "Fecha",
                            "vendedor": "Vendedor",
                            "tarea": "Tarea",
                            "tipo_brief": "Tipo",
                            "unit_amount": st.column_config.NumberColumn("Horas", format="%.1f"),
                            "name": "Descripción",
                        },
                    )