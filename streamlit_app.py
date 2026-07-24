# -*- coding: utf-8 -*-
"""
Dashboard comercial conectado a Odoo 19 vía XML-RPC.
Modelos: sale.order, account.move, crm.lead — segmentado por equipos de ventas (crm.team).
Pensado para desplegarse en Streamlit Community Cloud (share.streamlit.io).
"""

import threading
import xmlrpc.client
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

# ─────────────────────────────────────────────
# Configuración de página
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Dashboard Comercial · Odoo 19",
    page_icon="📊",
    layout="wide",
)

# ─────────────────────────────────────────────
# Conexión a Odoo (XML-RPC)
# ─────────────────────────────────────────────
# get_connection() se cachea con cache_resource → el mismo ServerProxy (y su
# conexión HTTP persistente) se comparte entre TODAS las sesiones de usuario.
# xmlrpc.client no es thread-safe sobre una conexión compartida, así que hay
# que serializar las llamadas con un lock para evitar http.client.ResponseNotReady
# cuando dos peticiones concurrentes reutilizan el mismo socket.
_odoo_lock = threading.Lock()


@st.cache_resource(show_spinner="Conectando con Odoo...")
def get_connection():
    """Autentica una sola vez y reutiliza la conexión en toda la sesión."""
    cfg = st.secrets["odoo"]
    common = xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/common", allow_none=True)
    with _odoo_lock:
        uid = common.authenticate(cfg["db"], cfg["username"], cfg["api_key"], {})
    if not uid:
        st.error("❌ Autenticación fallida. Verifica url, db, username y api_key en los Secrets.")
        st.stop()
    models = xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/object", allow_none=True)
    return cfg, uid, models


def odoo_call(model: str, method: str, args: list, kwargs: dict | None = None):
    cfg, uid, models = get_connection()
    with _odoo_lock:
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
        ["name", "date_closed", "user_id", "team_id", "expected_revenue"],
        order="date_closed",
    )
    if df.empty:
        return df
    df["date_closed"] = pd.to_datetime(df["date_closed"])
    df["mes_num"] = df["date_closed"].dt.month
    df["mes"] = df["date_closed"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    return df


@st.cache_data(ttl=600, show_spinner="Cargando ganados por línea...")
def load_won_lineas(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Oportunidades GANADAS con fecha de cierre en el período, explotadas por etiqueta
    (línea de negocio). Si una oportunidad tiene varias etiquetas, su valor se cuenta
    en cada una."""
    domain = [
        ("type", "=", "opportunity"),
        ("probability", "=", 100),
        ("date_closed", ">=", date_from),
        ("date_closed", "<=", f"{date_to} 23:59:59"),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    cols = ["name", "date_closed", "vendedor", "equipo", "expected_revenue", "mes", "linea"]
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
    df["linea"] = df["tag_ids"].apply(
        lambda ids: [tag_names.get(i, "Sin etiqueta") for i in ids] if ids else ["Sin etiqueta"])
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


def fmt_money(v: float) -> str:
    return f"${v:,.0f}"


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
team_names = st.sidebar.multiselect(
    "Equipos de venta",
    options=teams_df["name"].tolist(),
    default=[],
    placeholder="Todos los equipos",
)
team_ids = teams_df.loc[teams_df["name"].isin(team_names), "id"].tolist()

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

st.title("📊 Dashboard Comercial")
st.caption(f"Período: {date_from:%d/%m/%Y} → {date_to:%d/%m/%Y}"
           + (f" · Equipos: {', '.join(team_names)}" if team_names else " · Todos los equipos"))

tab_ventas, tab_fact, tab_crm, tab_leads, tab_metas, tab_lineas, tab_clientes, tab_soporte = st.tabs(
    ["🛒 Ventas", "🧾 Facturación", "🎯 CRM / Pipeline", "📨 Leads y Conversión",
     "🏆 Metas por vendedor", "🏷️ Metas por línea", "🧑‍🤝‍🧑 Clientes Nuevos vs. Recurrentes",
     "🛠️ Soporte y Mantenimiento"]
)

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

        with st.expander("📋 Detalle de oportunidades ganadas por línea"):
            st.dataframe(
                won_lineas[["name", "date_closed", "vendedor", "equipo", "linea", "expected_revenue"]],
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