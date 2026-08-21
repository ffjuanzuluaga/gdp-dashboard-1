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
    records = odoo_call(model, "search_read", [domain], {"fields": fields, **kw})
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=fields)
    return df


def m2o_name(series: pd.Series) -> pd.Series:
    """Convierte columnas many2one [id, 'nombre'] → 'nombre'."""
    return series.apply(lambda v: v[1] if isinstance(v, (list, tuple)) and len(v) == 2 else "Sin asignar")


def m2o_id(series: pd.Series) -> pd.Series:
    return series.apply(lambda v: v[0] if isinstance(v, (list, tuple)) else None)


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
         "amount_untaxed", "amount_total", "state"],
        order="date_order",
    )
    if df.empty:
        return df
    df["date_order"] = pd.to_datetime(df["date_order"])
    df["equipo"] = m2o_name(df["team_id"])
    df["vendedor"] = m2o_name(df["user_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    return df


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
        ["invoice_date", "partner_id", "amount_total_signed"],
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


@st.cache_data(ttl=600, show_spinner="Cargando facturación por categoría...")
def load_invoice_lines_by_categoria(categoria: str, team_ids: list[int]) -> pd.DataFrame:
    """TODO el historial de líneas de factura de una categoría de producto dada
    (sin límite de fecha), para poder clasificar la facturación por línea de
    negocio y saber desde cuándo se le factura a cada cliente."""
    domain = [
        ("move_id.move_type", "in", ["out_invoice", "out_refund"]),
        ("move_id.state", "=", "posted"),
        ("product_id.categ_id.name", "=", categoria),
    ]
    if team_ids:
        domain.append(("move_id.team_id", "in", team_ids))
    df = search_read(
        "account.move.line", domain,
        ["date", "partner_id", "price_subtotal", "move_type"],
        order="date",
    )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["monto"] = df.apply(
        lambda r: -r["price_subtotal"] if r["move_type"] == "out_refund" else r["price_subtotal"],
        axis=1,
    )
    df["mes"] = df["date"].dt.to_period("M").astype(str)
    df["cliente_id"] = m2o_id(df["partner_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    return df


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
        ["name", "invoice_date", "partner_id", "invoice_user_id", "team_id",
         "amount_untaxed_signed", "amount_total_signed", "amount_residual_signed",
         "payment_state", "move_type"],
        order="invoice_date",
    )
    if df.empty:
        return df
    df["invoice_date"] = pd.to_datetime(df["invoice_date"])
    df["equipo"] = m2o_name(df["team_id"])
    df["vendedor"] = m2o_name(df["invoice_user_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    return df


@st.cache_data(ttl=600, show_spinner="Cargando pipeline CRM...")
def load_leads(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    # active in (True, False) para incluir oportunidades perdidas
    domain = [
        ("create_date", ">=", date_from),
        ("create_date", "<=", f"{date_to} 23:59:59"),
        ("type", "=", "opportunity"),
        "|", ("active", "=", True), ("active", "=", False),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    df = search_read(
        "crm.lead",
        domain,
        ["name", "create_date", "date_closed", "partner_id", "user_id", "team_id",
         "stage_id", "expected_revenue", "probability", "active"],
        order="create_date",
    )
    if df.empty:
        return df
    df["create_date"] = pd.to_datetime(df["create_date"])
    df["equipo"] = m2o_name(df["team_id"])
    df["vendedor"] = m2o_name(df["user_id"])
    df["etapa"] = m2o_name(df["stage_id"])
    df["estado"] = df.apply(
        lambda r: "Ganada" if r["probability"] == 100
        else ("Perdida" if not r["active"] else "Abierta"),
        axis=1,
    )
    return df


@st.cache_data(ttl=600, show_spinner="Cargando metas...")
def load_metas() -> pd.DataFrame:
    """Metas anuales por vendedor desde metas.csv (en la raíz del repo).
    El nombre en la columna 'vendedor' debe coincidir EXACTAMENTE con el
    nombre del usuario en Odoo."""
    try:
        df = pd.read_csv("data/metas.csv")
        df["vendedor"] = df["vendedor"].str.strip()
        return df
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
    """Oportunidades GANADAS con fecha de cierre en el período (reporte de ganados)."""
    domain = [
        ("type", "=", "opportunity"),
        ("probability", "=", 100),
        ("date_closed", ">=", date_from),
        ("date_closed", "<=", f"{date_to} 23:59:59"),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
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
    return df


@st.cache_data(ttl=600, show_spinner="Cargando ganados por línea...")
def load_won_lineas(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Oportunidades GANADAS con fecha de cierre en el período, explotadas por etiqueta
    (línea de negocio). Si una oportunidad tiene varias etiquetas, su valor se cuenta
    en cada una. También expone si la etiqueta marca cliente nuevo (ver
    clasificar_tags_lineas)."""
    domain = [
        ("type", "=", "opportunity"),
        ("probability", "=", 100),
        ("date_closed", ">=", date_from),
        ("date_closed", "<=", f"{date_to} 23:59:59"),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
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
    return df[cols]


@st.cache_data(ttl=600, show_spinner="Cargando cumplimiento mensual por línea...")
def load_cumplimiento_mensual(year: int, team_ids: list[int]) -> pd.DataFrame:
    """Vendido (CRM ganado) y Facturado, por línea de negocio y mes, para el año
    dado. Una fila por (línea, mes) con columnas 'vendido' y 'facturado';
    incluye los 12 meses aunque no haya movimiento (quedan en 0)."""
    won = load_won_lineas(f"{year}-01-01", f"{year}-12-31", team_ids)
    if not won.empty:
        vendido = (won.groupby(["linea", "mes"], as_index=False)["expected_revenue"]
                   .sum().rename(columns={"expected_revenue": "vendido"}))
    else:
        vendido = pd.DataFrame(columns=["linea", "mes", "vendido"])

    partes = []
    for linea, categoria in LINEA_CATEGORIA.items():
        lineas_fact = load_invoice_lines_by_categoria(categoria, team_ids)
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
    tabla = base.merge(vendido, on=["linea", "mes"], how="left")
    tabla = tabla.merge(facturado, on=["linea", "mes"], how="left")
    tabla["vendido"] = tabla["vendido"].fillna(0)
    tabla["facturado"] = tabla["facturado"].fillna(0)
    return tabla


def construir_pivote_cumplimiento(datos: pd.DataFrame, value_col: str, year: int,
                                   metas_lineas: pd.DataFrame) -> pd.DataFrame:
    """Matriz línea (filas, + TOTAL) x mes (columnas, + Acumulado + % Cumpl.
    Anual), al estilo del reporte de referencia en Excel/Sheets."""
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
        fila["_meta_anual"] = meta_anual
        filas.append(fila)

    tabla = pd.DataFrame(filas)
    total = {"Línea": "TOTAL"}
    for nombre_mes in MESES_ES:
        total[nombre_mes] = tabla[nombre_mes].sum()
    total["Acumulado"] = tabla["Acumulado"].sum()
    meta_total = tabla["_meta_anual"].sum()
    total["% Cumpl. Anual"] = (total["Acumulado"] / meta_total * 100) if meta_total else 0.0
    tabla = tabla.drop(columns="_meta_anual")
    return pd.concat([tabla, pd.DataFrame([total])], ignore_index=True)


@st.cache_data(ttl=600, show_spinner="Cargando leads y pipeline completo...")
def load_leads_full(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Todo el pipeline (leads + oportunidades) con origen, para el reporte de conversión."""
    domain = [
        ("create_date", ">=", date_from),
        ("create_date", "<=", f"{date_to} 23:59:59"),
        "|", ("active", "=", True), ("active", "=", False),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    df = search_read(
        "crm.lead", domain,
        ["name", "create_date", "user_id", "team_id", "source_id",
         "expected_revenue", "probability", "active", "type"],
        order="create_date",
    )
    if df.empty:
        return df
    df["create_date"] = pd.to_datetime(df["create_date"])
    df["mes"] = df["create_date"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    df["origen"] = m2o_name(df["source_id"])
    df["estado"] = df.apply(
        lambda r: "Ganada" if r["probability"] == 100
        else ("Perdida" if not r["active"] else "Abierta"),
        axis=1,
    )
    return df


@st.cache_data(ttl=600, show_spinner="Cargando cierres del período...")
def load_pipeline_closed(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Oportunidades GANADAS o PERDIDAS con fecha de cierre en el período,
    sin importar cuándo se crearon (incluye leads heredados de períodos
    anteriores que se cerraron ahora). Se usa para la tasa de conversión."""
    domain = [
        ("type", "=", "opportunity"),
        ("date_closed", ">=", date_from),
        ("date_closed", "<=", f"{date_to} 23:59:59"),
        "|", ("active", "=", True), ("active", "=", False),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    df = search_read(
        "crm.lead", domain,
        ["name", "date_closed", "user_id", "team_id", "source_id",
         "expected_revenue", "probability", "active"],
        order="date_closed",
    )
    if df.empty:
        return df
    df["date_closed"] = pd.to_datetime(df["date_closed"])
    df["mes"] = df["date_closed"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    df["origen"] = m2o_name(df["source_id"])
    df["estado"] = df.apply(lambda r: "Ganada" if r["probability"] == 100 else "Perdida", axis=1)
    return df


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
    """Líneas de sale.order.line cuya unidad de medida es horas (nombre de la
    UoM contiene 'hora'), de órdenes confirmadas. Se cruza luego con `sales`
    (ya cargado para el mismo período) para saber mes/vendedor/equipo."""
    domain = [
        ("order_id.date_order", ">=", date_from),
        ("order_id.date_order", "<=", f"{date_to} 23:59:59"),
        ("order_id.state", "in", ["sale", "done"]),
        ("product_uom_id.name", "ilike", "hora"),
    ]
    if team_ids:
        domain.append(("order_id.team_id", "in", team_ids))
    cols = ["order_id", "product_uom_qty", "product_uom_id"]
    try:
        df = search_read("sale.order.line", domain, cols)
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo consultar horas vendidas (sale.order.line): {e}"
    if df.empty:
        return df, None
    df["order_id_num"] = m2o_id(df["order_id"])
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando actividades del equipo...")
def load_team_activities(team_ids: list[int]):
    """Actividades PENDIENTES (mail.activity) de crm.lead, por tipo y vendedor.
    OJO: Odoo elimina las actividades de mail.activity al marcarlas 'hecho' (se
    convierten en un mensaje en el chatter), así que esto es una FOTO del
    backlog actual — no un histórico mensual de actividades completadas. No
    incluye horas: mail.activity no trae duración en Odoo estándar."""
    cols = ["res_id", "activity_type_id", "user_id", "date_deadline", "create_date"]
    try:
        df = search_read(
            "mail.activity",
            [("res_model", "=", "crm.lead")],
            cols,
        )
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo consultar actividades (mail.activity): {e}"
    if df.empty:
        return df, None
    df["tipo"] = m2o_name(df["activity_type_id"])
    df["vendedor"] = m2o_name(df["user_id"])
    df["date_deadline"] = pd.to_datetime(df["date_deadline"])
    df["mes_vencimiento"] = df["date_deadline"].dt.to_period("M").astype(str)
    if team_ids:
        leads_ids = set(
            search_read("crm.lead", [("team_id", "in", team_ids)], ["id"])["id"].tolist()
        )
        df = df[df["res_id"].isin(leads_ids)]
    return df, None


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


def fmt_miles_apostrofe(v: float) -> str:
    """Valor en miles de pesos, con apóstrofe como separador (p. ej. 40'463 para
    $40.463.000), al estilo de notación informal colombiana."""
    return f"{v / 1000:,.0f}".replace(",", "'")


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

(tab_td, tab_ventas, tab_fact, tab_crm, tab_leads, tab_metas, tab_lineas, tab_clientes,
 tab_soporte, tab_rentabilidad, tab_equipo) = st.tabs(
    ["🏠 Resumen Ejecutivo", "🛒 Ventas", "🧾 Facturación", "🎯 CRM / Pipeline",
     "📨 Leads y Conversión", "🏆 Metas por vendedor", "🏷️ Metas por línea",
     "🧑‍🤝‍🧑 Clientes Nuevos vs. Recurrentes", "🛠️ Soporte y Mantenimiento",
     "💵 Rentabilidad", "🕐 Horas y Actividad del equipo"]
)

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

        st.markdown("#### 🎯 Resumen del año")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Meta de ventas año", fmt_money(meta_anual_td))
        c2.metric("Ventas acumuladas del año", fmt_money(vendido_anual_td))
        c3.metric("Facturación acumulada del año", fmt_money(facturado_anual_td))
        c4.metric("% cumplimiento anual", f"{pct_anual_td:.1f}%")

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
                st.plotly_chart(fig, use_container_width=True)
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
                st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.markdown("#### 🧾 Facturación mensual por tipo (Proyectos / Soporte / Licenciamiento)")
        fig = px.bar(datos_td, x="mes", y="facturado", color="linea", barmode="group",
                     title=f"Facturado por mes y línea — {anio_td}",
                     labels={"facturado": "COP", "mes": "Mes", "linea": "Línea"})
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("#### 🏆 Meta del año y total facturado por tipo")
        resumen_tipo = metas_lineas_td.merge(
            datos_td.groupby("linea", as_index=False)["facturado"].sum(),
            on="linea", how="left")
        resumen_tipo["facturado"] = resumen_tipo["facturado"].fillna(0)
        resumen_tipo["pct"] = resumen_tipo.apply(
            lambda r: r["facturado"] / r["meta_anual"] * 100 if r["meta_anual"] else 0, axis=1)
        st.dataframe(
            resumen_tipo, use_container_width=True, hide_index=True,
            column_config={
                "linea": "Línea",
                "meta_anual": st.column_config.NumberColumn("Meta año", format="$%,.0f"),
                "facturado": st.column_config.NumberColumn("Total facturado", format="$%,.0f"),
                "pct": st.column_config.ProgressColumn("% cumplimiento", format="%.1f%%",
                                                        min_value=0, max_value=150),
            },
        )

        st.caption("💡 Rentabilidad por línea (facturado vs. costos del equipo) está en la "
                  "pestaña **💵 Rentabilidad**.")

# ─────────────────────────────────────────────
# TAB 1: Ventas (sale.order)
# ─────────────────────────────────────────────
with tab_ventas:
    if sales.empty:
        st.info("No hay órdenes de venta confirmadas en el período seleccionado.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Órdenes confirmadas", len(sales))
        c2.metric("Ventas (sin imp.)", fmt_money(sales["amount_untaxed"].sum()))
        c3.metric("Ventas (total)", fmt_money(sales["amount_total"].sum()))
        c4.metric("Ticket promedio", fmt_money(sales["amount_total"].mean()))

        col_a, col_b = st.columns(2)
        with col_a:
            por_equipo = (sales.groupby("equipo", as_index=False)["amount_total"]
                          .sum().sort_values("amount_total", ascending=False))
            fig = px.bar(por_equipo, x="equipo", y="amount_total",
                         title="Ventas por equipo", text_auto=".2s",
                         labels={"amount_total": "Total", "equipo": "Equipo"})
            st.plotly_chart(fig, use_container_width=True)
        with col_b:
            mensual = (sales.assign(mes=sales["date_order"].dt.to_period("M").astype(str))
                       .groupby(["mes", "equipo"], as_index=False)["amount_total"].sum())
            fig = px.line(mensual, x="mes", y="amount_total", color="equipo",
                          markers=True, title="Evolución mensual por equipo",
                          labels={"amount_total": "Total", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)

        col_c, col_d = st.columns(2)
        with col_c:
            por_vendedor = (sales.groupby("vendedor", as_index=False)["amount_total"]
                            .sum().sort_values("amount_total", ascending=True).tail(10))
            fig = px.bar(por_vendedor, x="amount_total", y="vendedor", orientation="h",
                         title="Top 10 vendedores", text_auto=".2s",
                         labels={"amount_total": "Total", "vendedor": ""})
            st.plotly_chart(fig, use_container_width=True)
        with col_d:
            top_clientes = (sales.groupby("cliente", as_index=False)["amount_total"]
                            .sum().sort_values("amount_total", ascending=True).tail(10))
            fig = px.bar(top_clientes, x="amount_total", y="cliente", orientation="h",
                         title="Top 10 clientes", text_auto=".2s",
                         labels={"amount_total": "Total", "cliente": ""})
            st.plotly_chart(fig, use_container_width=True)

        mensual_vendedor = (sales.assign(mes=sales["date_order"].dt.to_period("M").astype(str))
                            .groupby(["mes", "vendedor"], as_index=False)["amount_total"].sum())
        fig = px.bar(mensual_vendedor, x="mes", y="amount_total", color="vendedor",
                     barmode="group", title="Ventas por vendedor y mes",
                     labels={"amount_total": "Total", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)

        with st.expander("📋 Detalle de órdenes"):
            st.dataframe(
                sales[["name", "date_order", "cliente", "vendedor", "equipo",
                       "amount_untaxed", "amount_total"]],
                use_container_width=True, hide_index=True,
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
                .groupby(["mes", "vendedor"], as_index=False)["amount_total"].sum())
            fig = px.bar(mensual_vendedor_12m, x="mes", y="amount_total", color="vendedor",
                         barmode="group", title="Ventas por vendedor y mes — últimos 12 meses",
                         labels={"amount_total": "COP", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)
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
            st.plotly_chart(fig, use_container_width=True)

# ─────────────────────────────────────────────
# TAB 2: Facturación (account.move)
# ─────────────────────────────────────────────
with tab_fact:
    if invoices.empty:
        st.info("No hay facturas publicadas en el período seleccionado.")
    else:
        facturado = invoices["amount_total_signed"].sum()
        pendiente = invoices["amount_residual_signed"].sum()
        nc = (invoices["move_type"] == "out_refund").sum()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Documentos", len(invoices))
        c2.metric("Facturado neto", fmt_money(facturado))
        c3.metric("Cartera pendiente", fmt_money(pendiente))
        c4.metric("Notas crédito", int(nc))

        col_a, col_b = st.columns(2)
        with col_a:
            por_equipo = (invoices.groupby("equipo", as_index=False)["amount_total_signed"]
                          .sum().sort_values("amount_total_signed", ascending=False))
            fig = px.bar(por_equipo, x="equipo", y="amount_total_signed",
                         title="Facturación neta por equipo", text_auto=".2s",
                         labels={"amount_total_signed": "Total", "equipo": "Equipo"})
            st.plotly_chart(fig, use_container_width=True)
        with col_b:
            estado_pago = (invoices.groupby("payment_state", as_index=False)["amount_total_signed"]
                           .sum())
            etiquetas = {"not_paid": "No pagada", "in_payment": "En pago",
                         "paid": "Pagada", "partial": "Parcial", "reversed": "Revertida"}
            estado_pago["estado"] = estado_pago["payment_state"].map(etiquetas).fillna(estado_pago["payment_state"])
            fig = px.pie(estado_pago, names="estado", values="amount_total_signed",
                         title="Distribución por estado de pago", hole=0.45)
            st.plotly_chart(fig, use_container_width=True)

        mensual = (invoices.assign(mes=invoices["invoice_date"].dt.to_period("M").astype(str))
                   .groupby(["mes", "equipo"], as_index=False)["amount_total_signed"].sum())
        fig = px.bar(mensual, x="mes", y="amount_total_signed", color="equipo",
                     barmode="group", title="Facturación mensual por equipo",
                     labels={"amount_total_signed": "Total", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)

        with st.expander("📋 Detalle de facturas"):
            st.dataframe(
                invoices[["name", "invoice_date", "cliente", "vendedor", "equipo",
                          "amount_total_signed", "amount_residual_signed", "payment_state"]],
                use_container_width=True, hide_index=True,
            )

    st.divider()
    st.markdown("### 🎯 Cumplimiento Meta vs. Vendido vs. Facturado por línea de negocio")
    st.caption("Esta sección usa un año calendario completo (no el rango de fechas del "
              "sidebar), para poder comparar los 12 meses a la vez, igual que el reporte "
              "de referencia en Excel/Sheets.")

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
        vendido_total = datos_cumpl["vendido"].sum()
        facturado_total = datos_cumpl["facturado"].sum()
        pct_anual_total = (facturado_total / meta_anual_total * 100) if meta_anual_total else 0
        diferencia_total = facturado_total - meta_anual_total

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Meta anual (3 líneas)", fmt_money(meta_anual_total))
        c2.metric("Vendido acumulado", fmt_money(vendido_total))
        c3.metric("Facturado acumulado", fmt_money(facturado_total))
        c4.metric("% cumplimiento anual", f"{pct_anual_total:.1f}%")
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

        consolidado_mensual = datos_cumpl.groupby("mes", as_index=False)[["vendido", "facturado"]].sum()
        consolidado_mensual["meta"] = meta_anual_total / 12
        consolidado_largo = consolidado_mensual.melt(
            id_vars="mes", value_vars=["meta", "vendido", "facturado"],
            var_name="concepto", value_name="valor")
        consolidado_largo["concepto"] = consolidado_largo["concepto"].map(
            {"meta": "Meta mensual", "vendido": "Vendido", "facturado": "Facturado"})
        fig = px.bar(consolidado_largo, x="mes", y="valor", color="concepto", barmode="group",
                     title="Meta vs. Vendido vs. Facturado por mes (consolidado, 3 líneas)",
                     color_discrete_map={"Meta mensual": "#9ca3af", "Vendido": "#f5a623",
                                         "Facturado": "#1f77b4"},
                     labels={"valor": "COP", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)

        fig = px.bar(datos_cumpl, x="mes", y="facturado", color="linea", barmode="group",
                     title="Facturado por mes y línea de negocio",
                     labels={"facturado": "COP", "mes": "Mes", "linea": "Línea"})
        st.plotly_chart(fig, use_container_width=True)

        col_config_pivote = {"Línea": "Línea"}
        for m in MESES_ES:
            col_config_pivote[m] = st.column_config.NumberColumn(m, format="$%,.0f")
        col_config_pivote["Acumulado"] = st.column_config.NumberColumn("Acumulado", format="$%,.0f")
        col_config_pivote["% Cumpl. Anual"] = st.column_config.ProgressColumn(
            "% Cumpl. Anual", format="%.1f%%", min_value=0, max_value=150)

        st.markdown("#### 📈 Ventas (CRM ganado) por línea y mes")
        pivote_ventas = construir_pivote_cumplimiento(datos_cumpl, "vendido", anio_cumpl, metas_lineas_cumpl)
        st.dataframe(pivote_ventas, use_container_width=True, hide_index=True,
                     column_config=col_config_pivote)

        st.markdown("#### 🧾 Facturado por línea y mes")
        pivote_facturado = construir_pivote_cumplimiento(datos_cumpl, "facturado", anio_cumpl, metas_lineas_cumpl)
        st.dataframe(pivote_facturado, use_container_width=True, hide_index=True,
                     column_config=col_config_pivote)

        st.markdown("#### ↕️ Diferencia mensual: Facturado vs. Meta, por línea")
        col_config_dif = {"Línea": "Línea"}
        for m in MESES_ES:
            col_config_dif[m] = st.column_config.NumberColumn(m, format="$%,.0f")
        col_config_dif["Acumulado"] = st.column_config.NumberColumn("Acumulado", format="$%,.0f")
        col_config_dif["% Cumpl. Anual"] = st.column_config.NumberColumn(
            "% variación vs. meta anual", format="%.1f%%")
        pivote_dif = construir_pivote_cumplimiento(datos_cumpl, "diferencia", anio_cumpl, metas_lineas_cumpl)
        st.dataframe(pivote_dif, use_container_width=True, hide_index=True,
                     column_config=col_config_dif)

        with st.expander("📋 Detalle mensual por línea (datos crudos)"):
            detalle = datos_cumpl.copy()
            detalle["pct_cumpl_venta"] = detalle.apply(
                lambda r: r["vendido"] / r["meta_mensual"] * 100 if r["meta_mensual"] else 0, axis=1)
            detalle["pct_cumpl_factura"] = detalle.apply(
                lambda r: r["facturado"] / r["meta_mensual"] * 100 if r["meta_mensual"] else 0, axis=1)
            st.dataframe(
                detalle[["linea", "mes", "meta_mensual", "vendido", "facturado",
                        "pct_cumpl_venta", "pct_cumpl_factura", "diferencia"]]
                    .sort_values(["linea", "mes"]),
                use_container_width=True, hide_index=True,
                column_config={
                    "linea": "Línea", "mes": "Mes",
                    "meta_mensual": st.column_config.NumberColumn("Meta mensual", format="$%,.0f"),
                    "vendido": st.column_config.NumberColumn("Vendido", format="$%,.0f"),
                    "facturado": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                    "pct_cumpl_venta": st.column_config.ProgressColumn(
                        "% Cumpl. Venta", format="%.1f%%", min_value=0, max_value=150),
                    "pct_cumpl_factura": st.column_config.ProgressColumn(
                        "% Cumpl. Factura", format="%.1f%%", min_value=0, max_value=150),
                    "diferencia": st.column_config.NumberColumn("Diferencia", format="$%,.0f"),
                },
            )

# ─────────────────────────────────────────────
# TAB 3: CRM (crm.lead)
# ─────────────────────────────────────────────
with tab_crm:
    if leads.empty:
        st.info("No hay oportunidades creadas en el período seleccionado.")
    else:
        abiertas = leads[leads["estado"] == "Abierta"]
        ganadas = leads[leads["estado"] == "Ganada"]
        perdidas = leads[leads["estado"] == "Perdida"]
        cerradas = len(ganadas) + len(perdidas)
        win_rate = len(ganadas) / cerradas * 100 if cerradas else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Oportunidades", len(leads))
        c2.metric("Pipeline abierto", fmt_money(abiertas["expected_revenue"].sum()))
        c3.metric("Ingresos ganados", fmt_money(ganadas["expected_revenue"].sum()))
        c4.metric("Tasa de conversión", f"{win_rate:.1f}%")

        col_a, col_b = st.columns(2)
        with col_a:
            embudo = (abiertas.groupby("etapa", as_index=False)
                      .agg(oportunidades=("name", "count"),
                           valor=("expected_revenue", "sum")))
            fig = px.funnel(embudo, x="valor", y="etapa",
                            title="Embudo de pipeline abierto (valor esperado)")
            st.plotly_chart(fig, use_container_width=True)
        with col_b:
            por_equipo = (leads.groupby(["equipo", "estado"], as_index=False)
                          .agg(n=("name", "count")))
            fig = px.bar(por_equipo, x="equipo", y="n", color="estado",
                         title="Oportunidades por equipo y estado",
                         color_discrete_map={"Ganada": "#2ca02c", "Perdida": "#d62728",
                                             "Abierta": "#1f77b4"},
                         labels={"n": "Oportunidades", "equipo": "Equipo"})
            st.plotly_chart(fig, use_container_width=True)

        por_vendedor = (leads.groupby(["vendedor", "estado"], as_index=False)
                        .agg(valor=("expected_revenue", "sum")))
        fig = px.bar(por_vendedor, x="vendedor", y="valor", color="estado",
                     title="Valor esperado por vendedor",
                     color_discrete_map={"Ganada": "#2ca02c", "Perdida": "#d62728",
                                         "Abierta": "#1f77b4"},
                     labels={"valor": "Valor esperado", "vendedor": ""})
        st.plotly_chart(fig, use_container_width=True)

        with st.expander("📋 Detalle de oportunidades"):
            st.dataframe(
                leads[["name", "create_date", "vendedor", "equipo", "etapa",
                       "expected_revenue", "probability", "estado"]],
                use_container_width=True, hide_index=True,
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
                   "números no son necesariamente el mismo grupo de registros.")

        if not leads_full.empty:
            mensual_vendedor = (leads_full.groupby(["mes", "vendedor"], as_index=False)
                                .agg(leads=("name", "count")))
            fig = px.bar(mensual_vendedor, x="mes", y="leads", color="vendedor",
                         barmode="group", title="Leads creados por vendedor y mes",
                         labels={"leads": "Leads", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)

            mensual_origen = (leads_full.groupby(["mes", "origen"], as_index=False)
                              .agg(leads=("name", "count")))
            fig = px.bar(mensual_origen, x="mes", y="leads", color="origen",
                         barmode="group", title="Leads creados por origen y mes",
                         labels={"leads": "Leads", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)

        col_a, col_b = st.columns(2)
        with col_a:
            if not leads_full.empty:
                por_origen = (leads_full.groupby("origen", as_index=False)
                              .agg(leads=("name", "count"))
                              .sort_values("leads", ascending=True).tail(10))
                fig = px.bar(por_origen, x="leads", y="origen", orientation="h",
                             title="Leads creados por origen", text_auto=True,
                             labels={"leads": "Leads", "origen": ""})
                st.plotly_chart(fig, use_container_width=True)
        with col_b:
            if not closed.empty:
                conv_vendedor = closed.groupby("vendedor", as_index=False).agg(
                    ganadas=("estado", lambda s: (s == "Ganada").sum()),
                    cerradas=("estado", "count"),
                )
                conv_vendedor["tasa_conversion"] = conv_vendedor.apply(
                    lambda r: r["ganadas"] / r["cerradas"] * 100 if r["cerradas"] else 0, axis=1)
                fig = px.bar(conv_vendedor.sort_values("tasa_conversion"),
                             x="tasa_conversion", y="vendedor", orientation="h",
                             title="Tasa de conversión por vendedor (por cierre)", text_auto=".1f",
                             labels={"tasa_conversion": "% conversión", "vendedor": ""})
                st.plotly_chart(fig, use_container_width=True)

        if not closed.empty:
            conv_origen = closed.groupby("origen", as_index=False).agg(
                ganadas=("estado", lambda s: (s == "Ganada").sum()),
                cerradas=("estado", "count"),
            )
            conv_origen["tasa_conversion"] = conv_origen.apply(
                lambda r: r["ganadas"] / r["cerradas"] * 100 if r["cerradas"] else 0, axis=1)
            st.markdown("#### Conversión por origen (por fecha de cierre)")
            st.dataframe(
                conv_origen.sort_values("cerradas", ascending=False),
                use_container_width=True, hide_index=True,
                column_config={
                    "origen": "Origen",
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
                                "estado", "expected_revenue"]],
                    use_container_width=True, hide_index=True,
                )
        if not closed.empty:
            with st.expander("📋 Detalle de cierres (ganados/perdidos) en el período"):
                st.dataframe(
                    closed[["name", "date_closed", "vendedor", "equipo", "origen",
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
        total_vendido = con_meta["vendido"].sum()
        c1.metric("Meta anual (equipo)", fmt_money(total_meta))
        c2.metric(f"Meta del período ({dias_periodo} días)", fmt_money(total_meta_periodo))
        c3.metric("Vendido (ganados)", fmt_money(total_vendido))
        c4.metric("Cumplimiento del período",
                  f"{total_vendido / total_meta_periodo * 100:.1f}%" if total_meta_periodo else "—")

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
            st.plotly_chart(fig, use_container_width=True)
        with col_b:
            mensual = (won.groupby(["mes", "vendedor"], as_index=False)["expected_revenue"].sum())
            fig = px.bar(mensual, x="mes", y="expected_revenue", color="vendedor",
                         title="Ganados por mes y vendedor (reporte de ganados)",
                         labels={"expected_revenue": "COP", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)

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

# ─────────────────────────────────────────────
# TAB: Metas por línea (ganados CRM clasificados por etiqueta vs metas_lineas.csv)
# ─────────────────────────────────────────────
with tab_lineas:
    metas_lineas = load_metas_lineas()
    won_lineas = load_won_lineas(d1, d2, team_ids)

    if metas_lineas.empty:
        st.warning("No se encontró `data/metas_lineas.csv`. Crea el archivo con columnas "
                   "`linea,meta_anual` en la raíz del repo (el nombre de `linea` debe "
                   "coincidir EXACTAMENTE con la etiqueta configurada en Odoo CRM).")
    elif won_lineas.empty:
        st.info("No hay oportunidades ganadas en el período seleccionado.")
    else:
        # Meta del período = meta anual prorrateada por los días del rango del sidebar
        dias_periodo_l = (date_to - date_from).days + 1

        ventas_linea = (won_lineas.groupby("linea", as_index=False)["expected_revenue"]
                        .sum().rename(columns={"expected_revenue": "vendido"}))

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
            st.warning("⚠️ Líneas con ventas pero SIN meta en metas_lineas.csv (verifica que "
                       f"el nombre coincida exactamente con la etiqueta en Odoo): {', '.join(sin_meta_l)}")

        con_meta_l = cumpl_l[cumpl_l["meta_anual"] > 0]

        c1, c2, c3, c4 = st.columns(4)
        total_meta_l = con_meta_l["meta_anual"].sum()
        total_meta_periodo_l = con_meta_l["meta_periodo"].sum()
        total_vendido_l = con_meta_l["vendido"].sum()
        c1.metric("Meta anual (todas las líneas)", fmt_money(total_meta_l))
        c2.metric(f"Meta del período ({dias_periodo_l} días)", fmt_money(total_meta_periodo_l))
        c3.metric("Vendido (ganados)", fmt_money(total_vendido_l))
        c4.metric("Cumplimiento del período",
                  f"{total_vendido_l / total_meta_periodo_l * 100:.1f}%" if total_meta_periodo_l else "—")

        tabla_l = con_meta_l[["linea", "meta_anual", "meta_periodo", "vendido",
                              "pct_periodo", "pct_anual"]].copy()
        st.dataframe(
            tabla_l,
            use_container_width=True, hide_index=True,
            column_config={
                "linea": "Línea",
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
            comp_l = con_meta_l.melt(
                id_vars="linea",
                value_vars=["meta_periodo", "vendido"],
                var_name="concepto", value_name="valor")
            comp_l["concepto"] = comp_l["concepto"].map(
                {"meta_periodo": "Meta del período", "vendido": "Vendido"})
            fig = px.bar(comp_l, x="linea", y="valor", color="concepto",
                         barmode="group", title="Vendido vs. meta del período por línea",
                         color_discrete_map={"Meta del período": "#9ca3af", "Vendido": "#1f77b4"},
                         labels={"valor": "COP", "linea": ""})
            st.plotly_chart(fig, use_container_width=True)
        with col_b:
            mensual_l = (won_lineas.groupby(["mes", "linea"], as_index=False)["expected_revenue"].sum())
            fig = px.bar(mensual_l, x="mes", y="expected_revenue", color="linea",
                         title="Ganados por mes y línea",
                         labels={"expected_revenue": "COP", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)

        st.markdown("#### Ganado por mes y vendedor, por línea")
        for linea_nombre in con_meta_l["linea"]:
            datos_linea = won_lineas[won_lineas["linea"] == linea_nombre]
            if datos_linea.empty:
                continue
            mensual_linea_vendedor = (datos_linea.groupby(["mes", "vendedor"], as_index=False)
                                      ["expected_revenue"].sum())
            fig = px.bar(mensual_linea_vendedor, x="mes", y="expected_revenue", color="vendedor",
                         barmode="group", title=f"{linea_nombre} — ganado por mes y vendedor",
                         labels={"expected_revenue": "COP", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)

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
            st.plotly_chart(fig, use_container_width=True)

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
                      ["amount_total_signed"].sum())
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
                st.plotly_chart(fig, use_container_width=True)
            with col_b:
                fact_seg = resumen.groupby("segmento", as_index=False)["amount_total_signed"].sum()
                fig = px.pie(fact_seg, names="segmento", values="amount_total_signed",
                            title="Facturación del período: nuevos vs. recurrentes", hole=0.45,
                            color="segmento",
                            color_discrete_map={"Nuevo": "#1f77b4", "Recurrente": "#9ca3af"})
                st.plotly_chart(fig, use_container_width=True)

            st.markdown("#### Comparativo de facturación: nuevos vs. recurrentes")
            orden_segmento = {"Nuevo": 0, "Recurrente": 1}
            fact_seg_tabla = fact_seg.sort_values(by="segmento", key=lambda s: s.map(orden_segmento))
            total_fact = fact_seg_tabla["amount_total_signed"].sum()
            fact_seg_tabla["pct_total"] = (
                fact_seg_tabla["amount_total_signed"] / total_fact * 100 if total_fact else 0.0)
            fila_total = pd.DataFrame([{
                "segmento": "Total",
                "amount_total_signed": total_fact,
                "pct_total": 100.0 if total_fact else 0.0,
            }])
            tabla_comp = pd.concat([fact_seg_tabla, fila_total], ignore_index=True)
            st.dataframe(
                tabla_comp,
                use_container_width=True, hide_index=True,
                column_config={
                    "segmento": "Segmento",
                    "amount_total_signed": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                    "pct_total": st.column_config.NumberColumn("% del total", format="%.1f%%"),
                },
            )

            with st.expander(f"📋 Clientes NUEVOS en el período ({len(nuevos)})"):
                st.dataframe(
                    nuevos[["cliente", "amount_total_signed"]]
                        .sort_values("amount_total_signed", ascending=False),
                    use_container_width=True, hide_index=True,
                    column_config={
                        "cliente": "Cliente",
                        "amount_total_signed": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
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
                                       "amount_total_signed"]]
                        .sort_values("amount_total_signed", ascending=False),
                    use_container_width=True, hide_index=True,
                    column_config={
                        "cliente": "Cliente",
                        "cliente_desde": "Cliente desde",
                        "antiguedad_anios": "Antigüedad (años)",
                        "amount_total_signed": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                    },
                )

    st.divider()
    st.markdown("#### 🏆 Top clientes del año")
    anio_top_clientes = st.selectbox("Año a analizar", options=range(hoy.year, hoy.year - 4, -1),
                                     index=0, key="anio_top_clientes")
    sales_anio = load_sales(f"{anio_top_clientes}-01-01", f"{anio_top_clientes}-12-31", team_ids)
    won_anio = load_won(f"{anio_top_clientes}-01-01", f"{anio_top_clientes}-12-31", team_ids)

    col_t1, col_t2 = st.columns(2)
    with col_t1:
        if sales_anio.empty:
            st.info(f"No hay ventas en {anio_top_clientes}.")
        else:
            top_ventas_anio = (sales_anio.groupby("cliente", as_index=False)["amount_total"]
                               .sum().sort_values("amount_total", ascending=True).tail(10))
            fig = px.bar(top_ventas_anio, x="amount_total", y="cliente", orientation="h",
                         title=f"Top 10 clientes por ventas — {anio_top_clientes}", text_auto=".2s",
                         labels={"amount_total": "Total", "cliente": ""})
            st.plotly_chart(fig, use_container_width=True)
    with col_t2:
        if won_anio.empty:
            st.info(f"No hay oportunidades ganadas en {anio_top_clientes}.")
        else:
            top_oport_anio = (won_anio.groupby("cliente", as_index=False)
                              .agg(oportunidades=("name", "count"))
                              .sort_values("oportunidades", ascending=True).tail(10))
            fig = px.bar(top_oport_anio, x="oportunidades", y="cliente", orientation="h",
                         title=f"Top 10 clientes por oportunidades ganadas — {anio_top_clientes}",
                         text_auto=True, labels={"oportunidades": "Oportunidades", "cliente": ""})
            st.plotly_chart(fig, use_container_width=True)

# ─────────────────────────────────────────────
# TAB: Soporte y Mantenimiento (categoría de producto "Soporte")
# ─────────────────────────────────────────────
with tab_soporte:
    soporte_lines = load_invoice_lines_by_categoria(LINEA_CATEGORIA["Soporte y Mantenimiento"], team_ids)

    if soporte_lines.empty:
        st.info("No hay facturación de la categoría 'Soporte' en el historial.")
    else:
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
            st.plotly_chart(fig, use_container_width=True)

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
# ─────────────────────────────────────────────
with tab_rentabilidad:
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
        st.plotly_chart(fig, use_container_width=True)

        if not costos_rent.empty:
            costo_mensual = costos_rent.groupby(["mes", "linea"], as_index=False)["costo"].sum()
            fact_mensual = datos_rent[["mes", "linea", "facturado"]]
            rent_mensual = fact_mensual.merge(costo_mensual, on=["mes", "linea"], how="left")
            rent_mensual["costo"] = rent_mensual["costo"].fillna(0)
            rent_mensual["rentabilidad"] = rent_mensual["facturado"] - rent_mensual["costo"]

            fig = px.bar(rent_mensual, x="mes", y="rentabilidad", color="linea", barmode="group",
                         title="Rentabilidad mensual por línea",
                         labels={"rentabilidad": "COP", "mes": "Mes", "linea": "Línea"})
            st.plotly_chart(fig, use_container_width=True)

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
    st.markdown("#### ⏱️ Horas vendidas mes a mes")
    st.caption("Suma de `product_uom_qty` en líneas de órdenes confirmadas cuya unidad de "
               "medida es horas, para el rango de fechas del sidebar.")
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

        c1, c2 = st.columns(2)
        c1.metric("Horas vendidas (período)", f"{horas_df['product_uom_qty'].sum():,.1f} h")
        c2.metric("Líneas", len(horas_df))

        col_h1, col_h2 = st.columns(2)
        with col_h1:
            mensual_horas = horas_df.groupby("mes", as_index=False)["product_uom_qty"].sum()
            fig = px.bar(mensual_horas, x="mes", y="product_uom_qty", text_auto=".1f",
                         title="Horas vendidas por mes",
                         labels={"product_uom_qty": "Horas", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)
        with col_h2:
            mensual_horas_vendedor = (horas_df.groupby(["mes", "vendedor"], as_index=False)
                                      ["product_uom_qty"].sum())
            fig = px.bar(mensual_horas_vendedor, x="mes", y="product_uom_qty", color="vendedor",
                         barmode="group", title="Horas vendidas por vendedor y mes",
                         labels={"product_uom_qty": "Horas", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.markdown("#### 👥 Actividades del equipo (backlog actual)")
    st.caption("⚠️ Odoo elimina las actividades (`mail.activity`) al marcarlas como hechas — "
               "se convierten en un mensaje en el historial del registro, no en un log "
               "consultable por mes. Por eso esto es una **foto del backlog pendiente hoy** "
               "por tipo y vendedor (agrupado por mes de vencimiento), no un histórico de "
               "actividades ya completadas. Tampoco incluye horas: `mail.activity` no trae "
               "duración en Odoo estándar. Si necesitan el histórico completo (presentación "
               "de negocio, propuesta, socialización de propuesta, seguimiento) con horas, "
               "hay que definir un registro dedicado — avísenme y lo armamos.")

    actividades, err_act = load_team_activities(team_ids)
    if err_act:
        st.warning(err_act)
    elif actividades.empty:
        st.info("No hay actividades pendientes sobre oportunidades (crm.lead) en este momento.")
    else:
        c1, c2 = st.columns(2)
        c1.metric("Actividades pendientes", len(actividades))
        c2.metric("Tipos distintos", actividades["tipo"].nunique())

        col_a1, col_a2 = st.columns(2)
        with col_a1:
            por_tipo_vend = (actividades.groupby(["tipo", "vendedor"], as_index=False)
                             .agg(cantidad=("res_id", "count")))
            fig = px.bar(por_tipo_vend, x="tipo", y="cantidad", color="vendedor",
                         barmode="group", title="Actividades pendientes por tipo y vendedor",
                         labels={"cantidad": "Actividades", "tipo": "Tipo"})
            st.plotly_chart(fig, use_container_width=True)
        with col_a2:
            por_mes_tipo = (actividades.groupby(["mes_vencimiento", "tipo"], as_index=False)
                            .agg(cantidad=("res_id", "count")))
            fig = px.bar(por_mes_tipo, x="mes_vencimiento", y="cantidad", color="tipo",
                         barmode="group", title="Actividades pendientes por mes de vencimiento",
                         labels={"cantidad": "Actividades", "mes_vencimiento": "Mes"})
            st.plotly_chart(fig, use_container_width=True)

        with st.expander("📋 Detalle de actividades pendientes"):
            st.dataframe(
                actividades[["tipo", "vendedor", "date_deadline", "create_date"]]
                    .sort_values("date_deadline"),
                use_container_width=True, hide_index=True,
                column_config={
                    "tipo": "Tipo", "vendedor": "Vendedor",
                    "date_deadline": "Vence", "create_date": "Creada",
                },
            )