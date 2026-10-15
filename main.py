# -*- coding: utf-8 -*-
"""
STAR BOT - bot Discord multifunzione in un unico file (discord.py 2.x)

Prefissi : star / Star / STAR  (es. "star kick @utente")  +  comandi slash "/"
Avvio    : pip install -U discord.py   ->   python main.py
Token    : variabile d'ambiente DISCORD_TOKEN (NON scriverlo nel codice e non metterlo su GitHub!)
Opzionali: GUILD_ID (sync immediato dei comandi slash in un server), DATA_FILE, SUPPORT_URL

Nel Developer Portal attiva gli Intent privilegiati: SERVER MEMBERS e MESSAGE CONTENT.
"""
import asyncio
import collections
import copy
import datetime as dt
import io
import json
import os
import random
import re
import time
import traceback
from typing import Literal, Optional

import aiohttp
import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID")
DATA_FILE = os.getenv("DATA_FILE", "data.json")
SUPPORT_URL = os.getenv("SUPPORT_URL", "")
COLOR = 0x5865F2
CURRENCY = "🪙"
MAX_TICKETS_PER_USER = 3
START_TIME = time.time()

# ============================================================================
#  STORAGE (JSON, salvataggio automatico)
# ============================================================================
DEFAULTS = {
    "warns": {}, "modlogs": {}, "welcome": {}, "goodbye": {}, "autorole": [],
    "antispam": {"enabled": False, "limit": 5, "seconds": 6},
    "antilink": {"enabled": False, "whitelist": []},
    "antiraid": {"enabled": False, "joins": 6, "seconds": 10},
    "raidmode": False,
    "verification": {"enabled": False, "role": None, "channel": None, "message_id": None,
                     "text": "Premi il pulsante qui sotto per verificarti e accedere al server."},
    "panels": {}, "ticket_counter": 0, "tickets": {}, "ticket_stats": {"closed": 0},
    "partners": {}, "partner_cfg": {"channel": None, "role": None, "next": 1,
                                    "requirements": "Nessun requisito impostato."},
    "economy": {}, "levels": {}, "afk": {}, "log_channel": None,
    "giveaways": {}, "reactionroles": {}, "rr_cfg": {"mode": "toggle"},
    "automod": {"enabled": False, "words": [], "logs": []},
    "staffperms": {}, "language": "it", "user_lang": {},
    "activity": {}, "stats": {"joins": 0, "leaves": 0, "messages": 0},
}


class Store:
    def __init__(self, path):
        self.path = path
        self.data = {"guilds": {}, "presence": None}
        self.dirty = False
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception as e:
                print(f"[store] impossibile leggere {path}: {e}")

    def touch(self):
        self.dirty = True

    def flush(self):
        if not self.dirty:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)
        self.dirty = False

    def guild(self, gid):
        g = self.data["guilds"].setdefault(str(gid), {})
        for k, v in DEFAULTS.items():
            if k not in g:
                g[k] = copy.deepcopy(v)
        return g


store = Store(DATA_FILE)


def gd(guild):
    return store.guild(guild.id if hasattr(guild, "id") else guild)


# ============================================================================
#  UTILITY
# ============================================================================
def emb(title=None, desc=None, color=COLOR):
    return discord.Embed(title=title, description=desc, color=color)


def cut(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def tstamp(sec, style="R"):
    return f"<t:{int(sec)}:{style}>"


def parse_duration(text):
    text = text.strip().lower().replace(" ", "")
    if not re.fullmatch(r"(\d+[smhdw])+", text):
        return None
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return sum(int(n) * mult[u] for n, u in re.findall(r"(\d+)([smhdw])", text))


async def fail(ctx, msg):
    await ctx.send(f"❌ {msg}", ephemeral=True)


def clean_emoji(s):
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"<a?:\w+:\d+>", s):
        return s
    if not any(c.isascii() and c.isalnum() for c in s) and len(s) <= 16:
        return s
    return None


def top_lines(guild, items, fmt, limit=10):
    out = []
    for i, (uid, val) in enumerate(items[:limit], 1):
        m = guild.get_member(int(uid))
        out.append(f"**{i}.** {m.display_name if m else uid} — {fmt(val)}")
    return "\n".join(out) or "Nessun dato."


# ============================================================================
#  PERMESSI (Discord + permessi interni assegnati ai ruoli con /staffperm)
# ============================================================================
INTERNAL_PERMS = {
    "warn_members": "Warn Members", "view_warnings": "View Warnings",
    "manage_warnings": "Manage Warnings", "view_modlogs": "View Mod Logs",
    "manage_announcements": "Manage Announcements", "manage_tickets": "Manage Tickets",
    "manage_partnerships": "Manage Partnerships", "manage_xp": "Manage XP",
    "use_say": "Use Say", "manage_embeds": "Manage Embeds",
    "manage_giveaways": "Manage Giveaways", "manage_logs": "Manage Guild Logs",
    "manage_automod": "Manage AutoMod", "view_automod_logs": "View AutoMod Logs",
    "manage_bot_status": "Manage Bot Status",
}


def has_perm(member, discord_perms=(), internal=()):
    if not isinstance(member, discord.Member):
        return False
    gp = member.guild_permissions
    if gp.administrator or any(getattr(gp, p) for p in discord_perms):
        return True
    if internal:
        cfg = gd(member.guild)["staffperms"]
        mine = {r.id for r in member.roles}
        for key in internal:
            if mine & set(cfg.get(key, [])):
                return True
    return False


class NoPerm(commands.CheckFailure):
    def __init__(self, needed):
        self.needed = needed
        super().__init__("permessi mancanti")


def require(discord_perms=(), internal=()):
    async def predicate(ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if has_perm(ctx.author, discord_perms, internal):
            return True
        needed = [p.replace("_", " ").title() for p in discord_perms] + [INTERNAL_PERMS[k] for k in internal]
        raise NoPerm(needed)
    return commands.check(predicate)


def can_act(ctx, target, allow_self=False):
    g = ctx.guild
    if target.id == ctx.author.id:
        return None if allow_self else "Non puoi farlo su te stesso."
    if target.id == g.owner_id:
        return "Non puoi agire sul proprietario del server."
    if ctx.author.id != g.owner_id and target.top_role >= ctx.author.top_role:
        return "Il suo ruolo è uguale o superiore al tuo."
    if target.top_role >= g.me.top_role:
        return "Il suo ruolo è uguale o superiore al mio."
    return None


def role_err(ctx, role):
    if role.is_default() or role.managed:
        return "Questo ruolo non può essere assegnato manualmente."
    if role >= ctx.guild.me.top_role:
        return "Il ruolo è più alto o uguale al mio ruolo più alto."
    if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
        return "Non puoi gestire un ruolo uguale o superiore al tuo."
    return None


async def send_log(guild, embed):
    ch_id = gd(guild)["log_channel"]
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass


async def mod_log(guild, user, action, mod, reason):
    d = gd(guild)
    d["modlogs"].setdefault(str(user.id), []).append(
        {"action": action, "mod": mod.id, "reason": reason, "ts": int(time.time())})
    store.touch()
    e = emb(f"🛡️ {action}", f"**Utente:** {user} (`{user.id}`)\n**Moderatore:** {mod.mention}\n**Motivo:** {reason}", 0xFEE75C)
    await send_log(guild, e)


# ============================================================================
#  GESTIONE ERRORI (prefisso e slash)
# ============================================================================
async def _reply_error(target, msg):
    try:
        if isinstance(target, discord.Interaction):
            if target.response.is_done():
                await target.followup.send(msg, ephemeral=True)
            else:
                await target.response.send_message(msg, ephemeral=True)
        else:
            await target.send(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def handle_error(target, error):
    root = error
    while getattr(root, "original", None) is not None and root.original is not root:
        root = root.original
    if getattr(root, "_star_done", False):
        return
    try:
        root._star_done = True
    except Exception:
        pass
    if isinstance(root, commands.CommandNotFound):
        return
    if isinstance(root, NoPerm):
        msg = "❌ Non hai i permessi necessari: " + ", ".join(root.needed)
    elif isinstance(root, commands.NoPrivateMessage):
        msg = "❌ Questo comando funziona solo nei server."
    elif isinstance(root, commands.MissingPermissions):
        msg = "❌ Ti mancano i permessi: " + ", ".join(root.missing_permissions)
    elif isinstance(root, commands.BotMissingPermissions):
        msg = "❌ Mi mancano i permessi: " + ", ".join(root.missing_permissions)
    elif isinstance(root, commands.MissingRequiredArgument):
        msg = f"❌ Manca l'argomento `{root.param.name}`."
    elif isinstance(root, commands.CommandOnCooldown):
        msg = f"⏳ Riprova tra {int(root.retry_after)} secondi."
    elif isinstance(root, (commands.BadArgument, commands.BadUnionArgument, commands.BadLiteralArgument,
                           commands.TooManyArguments, commands.RangeError)):
        msg = f"❌ Argomento non valido: {root}"
    elif isinstance(root, commands.CheckFailure):
        msg = "❌ Non puoi usare questo comando."
    elif isinstance(root, discord.Forbidden):
        msg = "❌ Non ho i permessi per farlo (controlla permessi e gerarchia dei ruoli)."
    else:
        traceback.print_exception(type(root), root, root.__traceback__)
        msg = "❌ Si è verificato un errore imprevisto."
    await _reply_error(target, msg)


# ============================================================================
#  TICKET - dati e utilità
# ============================================================================
BTN_STYLES = {"blu": discord.ButtonStyle.primary, "verde": discord.ButtonStyle.success,
              "grigio": discord.ButtonStyle.secondary, "rosso": discord.ButtonStyle.danger}
# azione: (etichetta, emoji, permesso predefinito, attivo di default)
ACTIONS = {
    "claim": ("Claim", "🙋", "staff", True),
    "unclaim": ("Unclaim", "↩️", "staff", True),
    "close": ("Close", "🔒", "aperto", True),
    "reopen": ("Reopen", "🔓", "staff", False),
    "transcript": ("Transcript", "📄", "staff", False),
    "add_member": ("Add Member", "➕", "staff", True),
    "remove_member": ("Remove Member", "➖", "staff", False),
    "rename": ("Rename", "✏️", "staff", False),
    "delete": ("Delete", "🗑️", "admin", False),
}
PERM_LABELS = {"tutti": "tutti", "aperto": "aperto (staff + chi l'ha aperto)", "staff": "staff", "admin": "admin"}
DEFAULT_CATEGORIES = [("Assistenza", "🛠️", "blu"), ("Partnership", "🤝", "verde"), ("Giveaway", "🎉", "grigio"),
                      ("Segnalazioni", "🚨", "rosso"), ("Altro", "❓", "grigio")]
CLOSE_LABELS = {"delete": "elimina il canale", "lock": "blocca il canale (con Reopen)"}
LOOK_LABELS = {"auto": "automatico (1 categoria = pulsante, 2 o più = menu a tendina)",
               "buttons": "pulsanti", "dropdown": "menu a tendina"}


def new_id():
    return os.urandom(3).hex()


def new_panel(name):
    return {
        "id": new_id(), "name": name, "title": "🎫 Assistenza",
        "description": "Scegli una categoria qui sotto per aprire un ticket.",
        "categories": [{"id": new_id(), "label": l, "emoji": e, "color": c, "description": "",
                        "discord_category": None, "staff_roles": [],
                        "welcome": "Benvenuto {user}! Descrivi il tuo problema: uno staffer ti risponderà a breve."}
                       for l, e, c in DEFAULT_CATEGORIES],
        "buttons": [{"id": k, "action": k, "label": v[0], "emoji": v[1], "perm": v[2], "enabled": v[3]}
                    for k, v in ACTIONS.items()],
        "discord_category": None, "close_mode": "delete", "appearance": "auto",
        "channel_id": None, "message_id": None, "color": COLOR,
    }


def panel_summary(guild, p):
    lines = [f"📌 **{p['title']}**", p["description"], "", "**Pulsanti esterni (categorie)**"]
    for i, c in enumerate(p["categories"], 1):
        lines.append(f"{i}. {c['emoji'] or ''} **{c['label']}** · {c['color']}")
    if not p["categories"]:
        lines.append("_nessuna categoria_")
    lines += ["", "**Pulsanti interni**"]
    for i, b in enumerate(p["buttons"], 1):
        lines.append(f"{i}. {b['emoji'] or ''} **{b['label']}** · {b['action']} · {b['perm']}"
                     + ("" if b["enabled"] else " · disattivato"))
    ch = f"<#{p['channel_id']}>" if p["channel_id"] else "_non pubblicato_"
    dc = guild.get_channel(p["discord_category"]) if p["discord_category"] else None
    lines += ["", "**Pubblicato in**", ch, "**Categoria Discord**", dc.name if dc else "_nessuna_",
              "**Close**", CLOSE_LABELS[p["close_mode"]], "**Aspetto**", LOOK_LABELS[p["appearance"]],
              "", "_Permessi: tutti, aperto (staff + chi l'ha aperto), staff, admin_"]
    return emb(f"⚙️ Editor · {p['name']}", cut("\n".join(lines), 4000), p["color"])


def panel_public_embed(p):
    return emb(p["title"], p["description"], p["color"])


def panel_public_view(p):
    v = discord.ui.View(timeout=None)
    cats = p["categories"]
    mode = p["appearance"]
    if mode == "auto":
        mode = "buttons" if len(cats) == 1 else "dropdown"
    if not cats:
        return v
    if mode == "buttons":
        for c in cats[:25]:
            v.add_item(discord.ui.Button(label=c["label"], emoji=c["emoji"] or None, style=BTN_STYLES[c["color"]],
                                         custom_id=f"tkt:open:{p['id']}:{c['id']}"))
    else:
        opts = [discord.SelectOption(label=c["label"][:100], value=c["id"], emoji=c["emoji"] or None,
                                     description=(c["description"] or None)) for c in cats[:25]]
        v.add_item(discord.ui.Select(placeholder="Scegli una categoria...", options=opts,
                                     custom_id=f"tkt:menu:{p['id']}"))
    return v


def ticket_view(t, p):
    v = discord.ui.View(timeout=None)
    for b in p["buttons"]:
        if not b["enabled"]:
            continue
        a = b["action"]
        if t["status"] == "open" and a == "reopen":
            continue
        if t["status"] == "closed" and a not in ("reopen", "transcript", "delete"):
            continue
        style = {"close": discord.ButtonStyle.danger, "delete": discord.ButtonStyle.danger,
                 "claim": discord.ButtonStyle.success}.get(a, discord.ButtonStyle.secondary)
        v.add_item(discord.ui.Button(label=b["label"], emoji=b["emoji"] or None, style=style,
                                     custom_id=f"tktb:{b['id']}"))
    return v


def ticket_staff_roles(guild, cat):
    ids = set(cat.get("staff_roles", []))
    for rid in gd(guild)["staffperms"].get("manage_tickets", []):
        ids.add(rid)
    return [r for r in (guild.get_role(i) for i in ids) if r]


def ticket_allowed(member, perm, t, cat):
    if perm == "tutti":
        return True
    gp = member.guild_permissions
    admin = gp.administrator or gp.manage_guild
    if perm == "admin":
        return admin
    mine = {r.id for r in member.roles}
    staff = admin or has_perm(member, (), ("manage_tickets",)) or bool(mine & set(cat.get("staff_roles", [])))
    if perm == "staff":
        return staff
    if perm == "aperto":
        return staff or member.id == t["owner"]
    return False


async def build_transcript(channel):
    lines = []
    async for m in channel.history(limit=5000, oldest_first=True):
        content = m.clean_content or ("[embed]" if m.embeds else "")
        atts = " ".join(a.url for a in m.attachments)
        lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author} ({m.author.id}): {content} {atts}".rstrip())
    data = "\n".join(lines) or "(vuoto)"
    return discord.File(io.BytesIO(data.encode("utf-8")), filename=f"transcript-{channel.name}.txt")


async def finalize_ticket(channel, t, closer, d):
    try:
        log_id = d["log_channel"]
        log_ch = channel.guild.get_channel(log_id) if log_id else None
        if log_ch:
            f = await build_transcript(channel)
            await log_ch.send(embed=emb("🎫 Ticket chiuso", f"**Canale:** {channel.name}\n**Aperto da:** <@{t['owner']}>\n"
                                        f"**Chiuso da:** {closer.mention}"), file=f)
    except Exception:
        traceback.print_exc()
    d["tickets"].pop(str(channel.id), None)
    d["ticket_stats"]["closed"] += 1
    store.touch()
    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket chiuso da {closer}")
    except discord.HTTPException:
        pass


async def open_ticket(interaction, pid, cid):
    guild, user = interaction.guild, interaction.user
    d = gd(guild)
    p = d["panels"].get(pid)
    cat = next((c for c in (p or {}).get("categories", []) if c["id"] == cid), None)
    if not p or not cat:
        return await interaction.response.send_message("❌ Questo pannello non è più valido.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    if p["appearance"] != "buttons" and not (p["appearance"] == "auto" and len(p["categories"]) == 1):
        try:  # azzera la selezione del menu a tendina
            await interaction.message.edit(view=panel_public_view(p))
        except discord.HTTPException:
            pass
    opened = sum(1 for t in d["tickets"].values() if t["owner"] == user.id and t["status"] == "open")
    if opened >= MAX_TICKETS_PER_USER:
        return await interaction.followup.send(f"❌ Hai già {opened} ticket aperti.", ephemeral=True)
    parent_id = cat.get("discord_category") or p.get("discord_category")
    parent = guild.get_channel(parent_id) if parent_id else None
    if not isinstance(parent, discord.CategoryChannel):
        parent = None
    d["ticket_counter"] += 1
    n = d["ticket_counter"]
    uname = re.sub(r"[^a-z0-9]", "", user.name.lower()) or "utente"
    perms = dict(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
    ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
          guild.me: discord.PermissionOverwrite(manage_channels=True, manage_messages=True, **perms),
          user: discord.PermissionOverwrite(**perms)}
    roles = ticket_staff_roles(guild, cat)
    for r in roles:
        ow[r] = discord.PermissionOverwrite(manage_messages=True, **perms)
    try:
        ch = await guild.create_text_channel(f"ticket-{uname}-{n}", category=parent, overwrites=ow,
                                             topic=f"Ticket di {user} ({user.id}) · {cat['label']}",
                                             reason=f"Ticket aperto da {user}")
    except discord.HTTPException as e:
        d["ticket_counter"] -= 1
        return await interaction.followup.send(f"❌ Non riesco a creare il canale: {e}", ephemeral=True)
    t = {"panel": pid, "cat": cid, "owner": user.id, "claimed": None, "status": "open",
         "number": n, "created": int(time.time())}
    d["tickets"][str(ch.id)] = t
    store.touch()
    e = emb(f"{cat['emoji'] or '🎫'} {cat['label']}", (cat.get("welcome") or "").replace("{user}", user.mention), p["color"])
    e.add_field(name="Aperto da", value=user.mention)
    e.add_field(name="Ticket", value=f"#{n}")
    ping = " ".join([user.mention] + [r.mention for r in roles])
    await ch.send(ping, embed=e, view=ticket_view(t, p), allowed_mentions=discord.AllowedMentions(roles=True, users=True))
    await interaction.followup.send(f"✅ Ticket creato: {ch.mention}", ephemeral=True)


class MemberPicker(discord.ui.View):
    def __init__(self, uid, channel, add):
        super().__init__(timeout=120)
        self.uid, self.channel, self.add = uid, channel, add
        sel = discord.ui.UserSelect(placeholder="Seleziona un utente", min_values=1, max_values=1)
        sel.callback = self.picked
        self.sel = sel
        self.add_item(sel)

    async def interaction_check(self, i):
        if i.user.id != self.uid:
            await i.response.send_message("Non è il tuo menu.", ephemeral=True)
            return False
        return True

    async def picked(self, i):
        m = self.sel.values[0]
        t = gd(i.guild)["tickets"].get(str(self.channel.id), {})
        if self.add:
            await self.channel.set_permissions(m, view_channel=True, send_messages=True,
                                               read_message_history=True, attach_files=True)
            txt = f"➕ {m.mention} è stato aggiunto al ticket da {i.user.mention}."
        else:
            if m.id == t.get("owner"):
                return await i.response.send_message("❌ Non puoi rimuovere chi ha aperto il ticket.", ephemeral=True)
            await self.channel.set_permissions(m, overwrite=None)
            txt = f"➖ {m.mention} è stato rimosso dal ticket da {i.user.mention}."
        await i.response.edit_message(content="✅ Fatto.", view=None)
        await self.channel.send(txt, allowed_mentions=discord.AllowedMentions.none())


class RenameModal(discord.ui.Modal, title="Rinomina ticket"):
    nome = discord.ui.TextInput(label="Nuovo nome", max_length=80)

    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, i):
        name = re.sub(r"[^a-z0-9\-_]", "-", self.nome.value.lower()).strip("-")[:90] or "ticket"
        await self.channel.edit(name=name)
        await i.response.send_message(f"✏️ Ticket rinominato in **{name}**.")


async def handle_ticket_button(interaction, bid):
    guild, user, ch = interaction.guild, interaction.user, interaction.channel
    d = gd(guild)
    t = d["tickets"].get(str(ch.id))
    if not t:
        return await interaction.response.send_message("❌ Ticket non registrato.", ephemeral=True)
    p = d["panels"].get(t["panel"])
    b = next((x for x in (p or {}).get("buttons", []) if x["id"] == bid), None)
    if not b or not b["enabled"]:
        return await interaction.response.send_message("❌ Pulsante non disponibile.", ephemeral=True)
    cat = next((c for c in p["categories"] if c["id"] == t["cat"]), {})
    if not ticket_allowed(user, b["perm"], t, cat):
        return await interaction.response.send_message("❌ Non hai il permesso per usare questo pulsante.", ephemeral=True)
    a = b["action"]
    if a == "claim":
        if t["claimed"] and t["claimed"] != user.id:
            return await interaction.response.send_message(f"❌ Già preso in carico da <@{t['claimed']}>.", ephemeral=True)
        if t["claimed"] == user.id:
            return await interaction.response.send_message("Hai già preso in carico questo ticket.", ephemeral=True)
        t["claimed"] = user.id
        store.touch()
        await interaction.response.send_message(embed=emb(None, f"🙋 {user.mention} ha preso in carico il ticket.", 0x57F287))
    elif a == "unclaim":
        if not t["claimed"]:
            return await interaction.response.send_message("Il ticket non è stato preso in carico.", ephemeral=True)
        if t["claimed"] != user.id and not ticket_allowed(user, "admin", t, cat):
            return await interaction.response.send_message("❌ Solo chi l'ha preso in carico (o un admin) può rilasciarlo.", ephemeral=True)
        t["claimed"] = None
        store.touch()
        await interaction.response.send_message(embed=emb(None, f"↩️ {user.mention} ha rilasciato il ticket.", 0xFEE75C))
    elif a == "close":
        if t["status"] != "open":
            return await interaction.response.send_message("Il ticket è già chiuso.", ephemeral=True)
        if p["close_mode"] == "delete":
            await interaction.response.send_message("🔒 Ticket chiuso: il canale verrà eliminato tra 5 secondi...")
            await finalize_ticket(ch, t, user, d)
        else:
            owner = guild.get_member(t["owner"])
            if owner:
                await ch.set_permissions(owner, send_messages=False, view_channel=True, read_message_history=True)
            t["status"] = "closed"
            store.touch()
            await interaction.response.send_message(embed=emb(None, f"🔒 Ticket chiuso da {user.mention}.", 0xED4245))
            try:
                await interaction.message.edit(view=ticket_view(t, p))
            except discord.HTTPException:
                pass
    elif a == "reopen":
        if t["status"] != "closed":
            return await interaction.response.send_message("Il ticket è già aperto.", ephemeral=True)
        owner = guild.get_member(t["owner"])
        if owner:
            await ch.set_permissions(owner, send_messages=True, view_channel=True, read_message_history=True)
        t["status"] = "open"
        store.touch()
        await interaction.response.send_message(embed=emb(None, f"🔓 Ticket riaperto da {user.mention}.", 0x57F287))
        try:
            await interaction.message.edit(view=ticket_view(t, p))
        except discord.HTTPException:
            pass
    elif a == "transcript":
        await interaction.response.defer(thinking=True)
        await interaction.followup.send("📄 Transcript del ticket:", file=await build_transcript(ch))
    elif a in ("add_member", "remove_member"):
        await interaction.response.send_message("Seleziona l'utente:", view=MemberPicker(user.id, ch, a == "add_member"),
                                                ephemeral=True)
    elif a == "rename":
        await interaction.response.send_modal(RenameModal(ch))
    elif a == "delete":
        await interaction.response.send_message("🗑️ Il ticket verrà eliminato tra 5 secondi...")
        await finalize_ticket(ch, t, user, d)


# ============================================================================
#  TICKET - editor del pannello (ticketpaneledit)
# ============================================================================
def cat_options(p):
    return [discord.SelectOption(label=c["label"][:100], value=c["id"], emoji=c["emoji"] or None,
                                 description=(c["description"] or c["color"])[:100]) for c in p["categories"]][:25]


def btn_options(p):
    return [discord.SelectOption(label=b["label"][:100], value=b["id"], emoji=b["emoji"] or None,
                                 description=f"{b['action']} · {b['perm']}" + ("" if b["enabled"] else " · disattivato"))
            for b in p["buttons"]][:25]


async def show_editor(interaction, owner_id, pid, note=None):
    p = gd(interaction.guild)["panels"].get(pid)
    if not p:
        return await interaction.response.edit_message(content="❌ Pannello non trovato.", embed=None, view=None)
    await interaction.response.edit_message(content=note, embed=panel_summary(interaction.guild, p),
                                            view=PanelEditor(owner_id, pid))


class BackButton(discord.ui.Button):
    def __init__(self, owner_id, pid, row=4):
        super().__init__(label="Indietro", emoji="⬅️", style=discord.ButtonStyle.secondary, row=row)
        self.owner_id, self.pid = owner_id, pid

    async def callback(self, i):
        if i.user.id != self.owner_id:
            return await i.response.send_message("Questo editor non è tuo.", ephemeral=True)
        await show_editor(i, self.owner_id, self.pid)


def picker_view(owner_id, pid, options, placeholder, on_pick):
    v = discord.ui.View(timeout=600)
    sel = discord.ui.Select(placeholder=placeholder, options=options)

    async def cb(i):
        if i.user.id != owner_id:
            return await i.response.send_message("Questo editor non è tuo.", ephemeral=True)
        await on_pick(i, sel.values[0])
    sel.callback = cb
    v.add_item(sel)
    v.add_item(BackButton(owner_id, pid, row=1))
    return v


async def open_picker(i, owner_id, pid, options, text, on_pick):
    if not options:
        return await i.response.send_message("❌ Non c'è nulla da scegliere.", ephemeral=True)
    await i.response.edit_message(content=text, embed=None, view=picker_view(owner_id, pid, options, "Scegli...", on_pick))


class CategoryModal(discord.ui.Modal):
    def __init__(self, owner_id, pid, cid=None):
        super().__init__(title="Categoria ticket" if not cid else "Modifica categoria")
        self.owner_id, self.pid, self.cid = owner_id, pid, cid
        c = next((x for x in gd_panel_cats(pid, owner_id) if x["id"] == cid), None) if cid else None
        self.nome = discord.ui.TextInput(label="Nome", max_length=80, default=c["label"] if c else None)
        self.emoji = discord.ui.TextInput(label="Emoji", required=False, max_length=64, default=c["emoji"] if c else None)
        self.colore = discord.ui.TextInput(label="Colore (blu, verde, grigio, rosso)", max_length=10,
                                           default=c["color"] if c else "blu")
        self.descr = discord.ui.TextInput(label="Descrizione (menu a tendina)", required=False, max_length=100,
                                          default=c["description"] if c else None)
        for x in (self.nome, self.emoji, self.colore, self.descr):
            self.add_item(x)

    async def on_submit(self, i):
        p = gd(i.guild)["panels"].get(self.pid)
        if not p:
            return await i.response.send_message("❌ Pannello non trovato.", ephemeral=True)
        color = self.colore.value.strip().lower()
        if color not in BTN_STYLES:
            return await i.response.send_message("❌ Colore non valido: usa blu, verde, grigio o rosso.", ephemeral=True)
        emoji = clean_emoji(self.emoji.value)
        if self.emoji.value.strip() and emoji is None:
            return await i.response.send_message("❌ Emoji non valida.", ephemeral=True)
        data = {"label": self.nome.value.strip(), "emoji": emoji, "color": color, "description": self.descr.value.strip()}
        if self.cid:
            c = next((x for x in p["categories"] if x["id"] == self.cid), None)
            if c:
                c.update(data)
        else:
            p["categories"].append({"id": new_id(), "discord_category": None, "staff_roles": [],
                                    "welcome": "Benvenuto {user}! Descrivi il tuo problema: uno staffer ti risponderà a breve.",
                                    **data})
        store.touch()
        await show_editor(i, self.owner_id, self.pid, "✅ Categoria salvata. Premi **Aggiorna pannello** per applicare.")


def gd_panel_cats(pid, owner_id):
    # cerca il pannello in tutti i server (serve solo a precompilare il modal)
    for g in store.data["guilds"].values():
        if pid in g.get("panels", {}):
            return g["panels"][pid]["categories"]
    return []


class ButtonModal(discord.ui.Modal, title="Pulsante interno"):
    def __init__(self, owner_id, pid, action, existing=None):
        super().__init__()
        self.owner_id, self.pid, self.action = owner_id, pid, action
        self.etichetta = discord.ui.TextInput(label="Etichetta", max_length=80,
                                              default=existing["label"] if existing else ACTIONS[action][0])
        self.emoji = discord.ui.TextInput(label="Emoji", required=False, max_length=64,
                                          default=(existing["emoji"] if existing else ACTIONS[action][1]))
        self.add_item(self.etichetta)
        self.add_item(self.emoji)

    async def on_submit(self, i):
        p = gd(i.guild)["panels"].get(self.pid)
        if not p:
            return await i.response.send_message("❌ Pannello non trovato.", ephemeral=True)
        emoji = clean_emoji(self.emoji.value)
        if self.emoji.value.strip() and emoji is None:
            return await i.response.send_message("❌ Emoji non valida.", ephemeral=True)
        b = next((x for x in p["buttons"] if x["id"] == self.action), None)
        if b:
            b.update(label=self.etichetta.value.strip(), emoji=emoji)
        else:
            a = ACTIONS[self.action]
            p["buttons"].append({"id": self.action, "action": self.action, "label": self.etichetta.value.strip(),
                                 "emoji": emoji, "perm": a[2], "enabled": True})
        store.touch()
        await show_editor(i, self.owner_id, self.pid, "✅ Pulsante salvato. Premi **Aggiorna pannello** per applicare.")


class TitleModal(discord.ui.Modal, title="Titolo e descrizione"):
    def __init__(self, owner_id, p):
        super().__init__()
        self.owner_id, self.pid = owner_id, p["id"]
        self.titolo = discord.ui.TextInput(label="Titolo", max_length=200, default=p["title"])
        self.descr = discord.ui.TextInput(label="Descrizione", style=discord.TextStyle.paragraph,
                                          max_length=2000, default=p["description"])
        self.add_item(self.titolo)
        self.add_item(self.descr)

    async def on_submit(self, i):
        p = gd(i.guild)["panels"].get(self.pid)
        if p:
            p["title"], p["description"] = self.titolo.value, self.descr.value
            store.touch()
        await show_editor(i, self.owner_id, self.pid, "✅ Testo aggiornato.")


class WelcomeModal(discord.ui.Modal, title="Messaggio di benvenuto"):
    def __init__(self, owner_id, pid, cid, current):
        super().__init__()
        self.owner_id, self.pid, self.cid = owner_id, pid, cid
        self.testo = discord.ui.TextInput(label="Testo ({user} = menzione)", style=discord.TextStyle.paragraph,
                                          max_length=1500, default=current)
        self.add_item(self.testo)

    async def on_submit(self, i):
        p = gd(i.guild)["panels"].get(self.pid)
        c = next((x for x in (p or {}).get("categories", []) if x["id"] == self.cid), None)
        if c:
            c["welcome"] = self.testo.value
            store.touch()
        await i.response.edit_message(content=cat_settings_text(i.guild, p, c),
                                      view=CatSettings(self.owner_id, self.pid, self.cid))


def cat_settings_text(guild, p, c):
    roles = " ".join(f"<@&{r}>" for r in c["staff_roles"]) or "_nessuno (solo admin e ruoli Manage Tickets)_"
    dc = f"<#{c['discord_category']}>" if c["discord_category"] else "_predefinita del pannello_"
    return (f"⚙️ **Impostazioni categoria: {c['label']}**\n**Ruoli staff:** {roles}\n"
            f"**Categoria Discord:** {dc}\n**Benvenuto:** {cut(c['welcome'], 200)}")


class CatSettings(discord.ui.View):
    def __init__(self, owner_id, pid, cid):
        super().__init__(timeout=600)
        self.owner_id, self.pid, self.cid = owner_id, pid, cid
        self.rs = discord.ui.RoleSelect(placeholder="Ruoli staff di questa categoria", min_values=1, max_values=25, row=0)
        self.cs = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.category], min_values=1, max_values=1,
                                           placeholder="Categoria Discord dei ticket", row=1)
        self.rs.callback = self.on_roles
        self.cs.callback = self.on_chan
        self.add_item(self.rs)
        self.add_item(self.cs)
        self.add_item(BackButton(owner_id, pid, row=2))

    def cat(self, i):
        p = gd(i.guild)["panels"].get(self.pid)
        return p, next((c for c in (p or {}).get("categories", []) if c["id"] == self.cid), None)

    async def interaction_check(self, i):
        if i.user.id != self.owner_id:
            await i.response.send_message("Questo editor non è tuo.", ephemeral=True)
            return False
        return True

    async def _refresh(self, i):
        p, c = self.cat(i)
        await i.response.edit_message(content=cat_settings_text(i.guild, p, c), view=CatSettings(self.owner_id, self.pid, self.cid))

    async def on_roles(self, i):
        p, c = self.cat(i)
        c["staff_roles"] = [r.id for r in self.rs.values]
        store.touch()
        await self._refresh(i)

    async def on_chan(self, i):
        p, c = self.cat(i)
        c["discord_category"] = self.cs.values[0].id
        store.touch()
        await self._refresh(i)

    @discord.ui.button(label="Messaggio di benvenuto", emoji="💬", row=2)
    async def b_welcome(self, i, b):
        p, c = self.cat(i)
        await i.response.send_modal(WelcomeModal(self.owner_id, self.pid, self.cid, c["welcome"]))

    @discord.ui.button(label="Azzera ruoli e categoria", emoji="♻️", style=discord.ButtonStyle.danger, row=2)
    async def b_reset(self, i, b):
        p, c = self.cat(i)
        c["staff_roles"], c["discord_category"] = [], None
        store.touch()
        await self._refresh(i)


class DiscordCategoryView(discord.ui.View):
    def __init__(self, owner_id, pid):
        super().__init__(timeout=600)
        self.owner_id, self.pid = owner_id, pid
        self.cs = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.category], min_values=1, max_values=1,
                                           placeholder="Scegli la categoria Discord", row=0)
        self.cs.callback = self.picked
        self.add_item(self.cs)
        self.add_item(BackButton(owner_id, pid, row=1))

    async def interaction_check(self, i):
        if i.user.id != self.owner_id:
            await i.response.send_message("Questo editor non è tuo.", ephemeral=True)
            return False
        return True

    async def picked(self, i):
        p = gd(i.guild)["panels"].get(self.pid)
        p["discord_category"] = self.cs.values[0].id
        store.touch()
        await show_editor(i, self.owner_id, self.pid, "✅ Categoria Discord impostata.")

    @discord.ui.button(label="Nessuna", style=discord.ButtonStyle.danger, row=1)
    async def none(self, i, b):
        gd(i.guild)["panels"][self.pid]["discord_category"] = None
        store.touch()
        await show_editor(i, self.owner_id, self.pid, "✅ Categoria Discord rimossa.")


class ButtonSettings(discord.ui.View):
    def __init__(self, owner_id, pid, bid):
        super().__init__(timeout=600)
        self.owner_id, self.pid, self.bid = owner_id, pid, bid
        sel = discord.ui.Select(placeholder="Chi può usare il pulsante", row=0,
                                options=[discord.SelectOption(label=v, value=k) for k, v in PERM_LABELS.items()])
        sel.callback = self.on_perm
        self.sel = sel
        self.add_item(sel)
        self.add_item(BackButton(owner_id, pid, row=1))

    def btn(self, i):
        return next(b for b in gd(i.guild)["panels"][self.pid]["buttons"] if b["id"] == self.bid)

    async def interaction_check(self, i):
        if i.user.id != self.owner_id:
            await i.response.send_message("Questo editor non è tuo.", ephemeral=True)
            return False
        return True

    def text(self, b):
        return (f"⚙️ **Impostazioni pulsante: {b['label']}**\nPermesso: **{PERM_LABELS[b['perm']]}**\n"
                f"Stato: **{'attivo' if b['enabled'] else 'disattivato'}**")

    async def on_perm(self, i):
        b = self.btn(i)
        b["perm"] = self.sel.values[0]
        store.touch()
        await i.response.edit_message(content=self.text(b), view=ButtonSettings(self.owner_id, self.pid, self.bid))

    @discord.ui.button(label="Attiva / Disattiva", emoji="🔀", style=discord.ButtonStyle.primary, row=1)
    async def toggle(self, i, button):
        b = self.btn(i)
        b["enabled"] = not b["enabled"]
        store.touch()
        await i.response.edit_message(content=self.text(b), view=ButtonSettings(self.owner_id, self.pid, self.bid))


class PublishView(discord.ui.View):
    def __init__(self, owner_id, pid):
        super().__init__(timeout=600)
        self.owner_id, self.pid = owner_id, pid
        self.cs = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], min_values=1, max_values=1,
                                           placeholder="Scegli il canale dove pubblicare", row=0)
        self.cs.callback = self.picked
        self.add_item(self.cs)
        self.add_item(BackButton(owner_id, pid, row=1))

    async def interaction_check(self, i):
        if i.user.id != self.owner_id:
            await i.response.send_message("Questo editor non è tuo.", ephemeral=True)
            return False
        return True

    async def picked(self, i):
        p = gd(i.guild)["panels"].get(self.pid)
        ch = i.guild.get_channel(self.cs.values[0].id)
        err = await publish_panel(i.guild, p, ch)
        await show_editor(i, self.owner_id, self.pid, f"❌ {err}" if err else f"✅ Pannello pubblicato in {ch.mention}.")


async def publish_panel(guild, p, channel):
    if not p["categories"]:
        return "Aggiungi almeno una categoria prima di pubblicare."
    if not isinstance(channel, discord.TextChannel):
        return "Scegli un canale di testo."
    pm = channel.permissions_for(guild.me)
    if not (pm.view_channel and pm.send_messages and pm.embed_links):
        return "Non posso scrivere (o inviare embed) in quel canale."
    old_ch = guild.get_channel(p["channel_id"]) if p["channel_id"] else None
    if old_ch and p["message_id"]:
        try:
            await (await old_ch.fetch_message(p["message_id"])).delete()
        except discord.HTTPException:
            pass
    msg = await channel.send(embed=panel_public_embed(p), view=panel_public_view(p))
    p["channel_id"], p["message_id"] = channel.id, msg.id
    store.touch()
    return None


async def refresh_panel_message(guild, p):
    ch = guild.get_channel(p["channel_id"]) if p["channel_id"] else None
    if not ch:
        return "Il pannello non è ancora pubblicato: usa **Pubblica in un canale**."
    try:
        msg = await ch.fetch_message(p["message_id"])
        await msg.edit(embed=panel_public_embed(p), view=panel_public_view(p))
    except discord.NotFound:
        return "Messaggio del pannello non trovato: ripubblicalo."
    return None


class PanelEditor(discord.ui.View):
    def __init__(self, owner_id, pid):
        super().__init__(timeout=1800)
        self.owner_id, self.pid = owner_id, pid

    async def interaction_check(self, i):
        if i.user.id != self.owner_id:
            await i.response.send_message("Questo editor non è tuo.", ephemeral=True)
            return False
        if self.pid not in gd(i.guild)["panels"]:
            await i.response.edit_message(content="❌ Pannello eliminato.", embed=None, view=None)
            return False
        return True

    def p(self, i):
        return gd(i.guild)["panels"][self.pid]

    # ---- riga 0: categorie
    @discord.ui.button(label="Aggiungi categoria", emoji="➕", style=discord.ButtonStyle.success, row=0)
    async def add_cat(self, i, b):
        if len(self.p(i)["categories"]) >= 25:
            return await i.response.send_message("❌ Massimo 25 categorie.", ephemeral=True)
        await i.response.send_modal(CategoryModal(self.owner_id, self.pid))

    @discord.ui.button(label="Modifica categoria", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def edit_cat(self, i, b):
        async def pick(inter, v):
            await inter.response.send_modal(CategoryModal(self.owner_id, self.pid, v))
        await open_picker(i, self.owner_id, self.pid, cat_options(self.p(i)), "Quale categoria vuoi modificare?", pick)

    @discord.ui.button(label="Impostazioni categoria", emoji="⚙️", style=discord.ButtonStyle.secondary, row=0)
    async def set_cat(self, i, b):
        async def pick(inter, v):
            p = self.p(inter)
            c = next(x for x in p["categories"] if x["id"] == v)
            await inter.response.edit_message(content=cat_settings_text(inter.guild, p, c),
                                              view=CatSettings(self.owner_id, self.pid, v))
        await open_picker(i, self.owner_id, self.pid, cat_options(self.p(i)), "Di quale categoria?", pick)

    @discord.ui.button(label="Rimuovi categoria", emoji="🗑️", style=discord.ButtonStyle.danger, row=0)
    async def rem_cat(self, i, b):
        async def pick(inter, v):
            p = self.p(inter)
            p["categories"] = [c for c in p["categories"] if c["id"] != v]
            store.touch()
            await show_editor(inter, self.owner_id, self.pid, "🗑️ Categoria rimossa.")
        await open_picker(i, self.owner_id, self.pid, cat_options(self.p(i)), "Quale categoria vuoi rimuovere?", pick)

    # ---- riga 1: impostazioni pannello
    @discord.ui.button(label="Titolo e descrizione", emoji="📝", style=discord.ButtonStyle.secondary, row=1)
    async def title(self, i, b):
        await i.response.send_modal(TitleModal(self.owner_id, self.p(i)))

    @discord.ui.button(label="Categoria Discord", emoji="📁", style=discord.ButtonStyle.secondary, row=1)
    async def dcat(self, i, b):
        await i.response.edit_message(content="Scegli la categoria Discord in cui creare i ticket:", embed=None,
                                      view=DiscordCategoryView(self.owner_id, self.pid))

    @discord.ui.button(label="Modalità Close", emoji="🔒", style=discord.ButtonStyle.secondary, row=1)
    async def close_mode(self, i, b):
        async def pick(inter, v):
            p = self.p(inter)
            p["close_mode"] = v
            if v == "lock":
                for x in p["buttons"]:
                    if x["action"] == "reopen":
                        x["enabled"] = True
            store.touch()
            await show_editor(inter, self.owner_id, self.pid, "✅ Modalità Close aggiornata.")
        opts = [discord.SelectOption(label=v, value=k) for k, v in CLOSE_LABELS.items()]
        await open_picker(i, self.owner_id, self.pid, opts, "Cosa succede quando si chiude un ticket?", pick)

    @discord.ui.button(label="Aspetto pannello", emoji="🎛️", style=discord.ButtonStyle.secondary, row=1)
    async def look(self, i, b):
        async def pick(inter, v):
            self.p(inter)["appearance"] = v
            store.touch()
            await show_editor(inter, self.owner_id, self.pid, "✅ Aspetto aggiornato. Premi **Aggiorna pannello**.")
        opts = [discord.SelectOption(label=v[:100], value=k) for k, v in LOOK_LABELS.items()]
        await open_picker(i, self.owner_id, self.pid, opts, "Come devono apparire le categorie?", pick)

    # ---- riga 2: pulsanti interni
    @discord.ui.button(label="Aggiungi pulsante", emoji="➕", style=discord.ButtonStyle.success, row=2)
    async def add_btn(self, i, b):
        used = {x["id"] for x in self.p(i)["buttons"]}
        opts = [discord.SelectOption(label=v[0], value=k, emoji=v[1]) for k, v in ACTIONS.items() if k not in used]

        async def pick(inter, v):
            await inter.response.send_modal(ButtonModal(self.owner_id, self.pid, v))
        await open_picker(i, self.owner_id, self.pid, opts, "Quale pulsante vuoi aggiungere?", pick)

    @discord.ui.button(label="Modifica pulsante", emoji="✏️", style=discord.ButtonStyle.primary, row=2)
    async def edit_btn(self, i, b):
        async def pick(inter, v):
            ex = next(x for x in self.p(inter)["buttons"] if x["id"] == v)
            await inter.response.send_modal(ButtonModal(self.owner_id, self.pid, ex["action"], ex))
        await open_picker(i, self.owner_id, self.pid, btn_options(self.p(i)), "Quale pulsante vuoi modificare?", pick)

    @discord.ui.button(label="Impostazioni pulsante", emoji="⚙️", style=discord.ButtonStyle.secondary, row=2)
    async def set_btn(self, i, b):
        async def pick(inter, v):
            view = ButtonSettings(self.owner_id, self.pid, v)
            await inter.response.edit_message(content=view.text(view.btn(inter)), view=view)
        await open_picker(i, self.owner_id, self.pid, btn_options(self.p(i)), "Di quale pulsante?", pick)

    @discord.ui.button(label="Rimuovi pulsante", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def rem_btn(self, i, b):
        async def pick(inter, v):
            p = self.p(inter)
            p["buttons"] = [x for x in p["buttons"] if x["id"] != v]
            store.touch()
            await show_editor(inter, self.owner_id, self.pid, "🗑️ Pulsante rimosso.")
        await open_picker(i, self.owner_id, self.pid, btn_options(self.p(i)), "Quale pulsante vuoi rimuovere?", pick)

    # ---- riga 3: azioni
    @discord.ui.button(label="Riepilogo", emoji="📋", style=discord.ButtonStyle.secondary, row=3)
    async def summary(self, i, b):
        await show_editor(i, self.owner_id, self.pid)

    @discord.ui.button(label="Aggiorna pannello", emoji="🔄", style=discord.ButtonStyle.success, row=3)
    async def update(self, i, b):
        err = await refresh_panel_message(i.guild, self.p(i))
        await i.response.send_message(f"❌ {err}" if err else "✅ Pannello aggiornato.", ephemeral=True)

    @discord.ui.button(label="Pubblica in un canale", emoji="📣", style=discord.ButtonStyle.primary, row=3)
    async def publish(self, i, b):
        await i.response.edit_message(content="Scegli il canale in cui pubblicare il pannello:", embed=None,
                                      view=PublishView(self.owner_id, self.pid))


# ============================================================================
#  1. MODERAZIONE
# ============================================================================
class Moderation(commands.Cog, name="Moderazione"):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="kick")
    @require(("kick_members",))
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = "Nessun motivo"):
        """Espelle un membro dal server."""
        if err := can_act(ctx, member):
            return await fail(ctx, err)
        await member.kick(reason=f"{ctx.author}: {reason}")
        await mod_log(ctx.guild, member, "kick", ctx.author, reason)
        await ctx.send(embed=emb("👢 Membro espulso", f"{member.mention} — {reason}"))

    @commands.hybrid_command(name="timeout")
    @require(("moderate_members",))
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(self, ctx, member: discord.Member, durata: str, *, reason: str = "Nessun motivo"):
        """Mette in timeout un membro (es. durata: 10m, 2h, 1d)."""
        if err := can_act(ctx, member):
            return await fail(ctx, err)
        secs = parse_duration(durata)
        if not secs or secs > 28 * 86400:
            return await fail(ctx, "Durata non valida (es. `30m`, `2h`, `1d`, massimo 28 giorni).")
        await member.timeout(discord.utils.utcnow() + dt.timedelta(seconds=secs), reason=f"{ctx.author}: {reason}")
        await mod_log(ctx.guild, member, f"timeout ({durata})", ctx.author, reason)
        await ctx.send(embed=emb("🔇 Timeout", f"{member.mention} per **{durata}** — {reason}"))

    @commands.hybrid_command(name="untimeout", aliases=["untimeot"])
    @require(("moderate_members",))
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(self, ctx, member: discord.Member):
        """Rimuove il timeout a un membro."""
        await member.timeout(None, reason=f"Rimosso da {ctx.author}")
        await mod_log(ctx.guild, member, "untimeout", ctx.author, "-")
        await ctx.send(embed=emb("🔊 Timeout rimosso", member.mention))

    @commands.hybrid_group(name="warn", invoke_without_command=True)
    async def warn(self, ctx):
        """Gestione avvertimenti: add, list, clear."""
        await ctx.send("Usa `warn add`, `warn list` o `warn clear`.")

    @warn.command(name="add")
    @require((), ("warn_members",))
    async def warn_add(self, ctx, member: discord.Member, *, reason: str = "Nessun motivo"):
        """Aggiunge un avvertimento a un membro."""
        if err := can_act(ctx, member):
            return await fail(ctx, err)
        w = gd(ctx.guild)["warns"].setdefault(str(member.id), [])
        w.append({"reason": reason, "mod": ctx.author.id, "ts": int(time.time())})
        store.touch()
        await mod_log(ctx.guild, member, "warn", ctx.author, reason)
        await ctx.send(embed=emb("⚠️ Avvertimento", f"{member.mention} — {reason}\nTotale avvertimenti: **{len(w)}**", 0xFEE75C))

    @warn.command(name="list")
    @require((), ("warn_members", "view_warnings"))
    async def warn_list(self, ctx, member: discord.Member):
        """Mostra gli avvertimenti di un membro."""
        w = gd(ctx.guild)["warns"].get(str(member.id), [])
        if not w:
            return await ctx.send(f"{member.mention} non ha avvertimenti.", ephemeral=True)
        lines = [f"**{i}.** {tstamp(x['ts'], 'd')} — {x['reason']} (da <@{x['mod']}>)" for i, x in enumerate(w, 1)]
        await ctx.send(embed=emb(f"⚠️ Avvertimenti di {member.display_name}", cut("\n".join(lines), 4000)), ephemeral=True)

    @warn.command(name="clear")
    @require((), ("warn_members", "manage_warnings"))
    async def warn_clear(self, ctx, member: discord.Member):
        """Cancella gli avvertimenti di un membro."""
        n = len(gd(ctx.guild)["warns"].pop(str(member.id), []))
        store.touch()
        await mod_log(ctx.guild, member, "warn clear", ctx.author, f"{n} avvertimenti cancellati")
        await ctx.send(f"🧹 Cancellati **{n}** avvertimenti a {member.mention}.")

    @commands.hybrid_command(name="purge")
    @require(("manage_messages",))
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(self, ctx, amount: commands.Range[int, 1, 500], member: Optional[discord.Member] = None):
        """Elimina in blocco un certo numero di messaggi."""
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        else:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
        chk = (lambda m: m.author.id == member.id) if member else None
        deleted = await ctx.channel.purge(limit=amount, check=chk)
        await mod_log(ctx.guild, ctx.author, "purge", ctx.author, f"{len(deleted)} messaggi in #{ctx.channel}")
        if ctx.interaction:
            await ctx.send(f"🧹 Eliminati **{len(deleted)}** messaggi.", ephemeral=True)
        else:
            await ctx.send(f"🧹 Eliminati **{len(deleted)}** messaggi.", delete_after=5)

    @commands.hybrid_group(name="slowmode", invoke_without_command=True)
    async def slowmode(self, ctx):
        """Gestione slowmode."""
        await ctx.send("Usa `slowmode set <secondi> [canale]`.")

    @slowmode.command(name="set")
    @require(("manage_channels",))
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode_set(self, ctx, secondi: commands.Range[int, 0, 21600], canale: Optional[discord.TextChannel] = None):
        """Imposta l'intervallo minimo tra i messaggi (0 = disattivo)."""
        ch = canale or ctx.channel
        await ch.edit(slowmode_delay=secondi, reason=f"Slowmode da {ctx.author}")
        await ctx.send(f"🐢 Slowmode in {ch.mention}: **{secondi}s**" if secondi else f"🐇 Slowmode disattivato in {ch.mention}.")

    @commands.hybrid_command(name="lock")
    @require(("manage_channels",))
    @commands.bot_has_permissions(manage_channels=True)
    async def lock(self, ctx, canale: Optional[discord.TextChannel] = None):
        """Blocca l'invio di messaggi nel canale."""
        ch = canale or ctx.channel
        ow = ch.overwrites_for(ctx.guild.default_role)
        ow.send_messages = False
        await ch.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"Lock da {ctx.author}")
        await ctx.send(f"🔒 {ch.mention} bloccato.")

    @commands.hybrid_command(name="unlock")
    @require(("manage_channels",))
    @commands.bot_has_permissions(manage_channels=True)
    async def unlock(self, ctx, canale: Optional[discord.TextChannel] = None):
        """Riabilita l'invio di messaggi nel canale."""
        ch = canale or ctx.channel
        ow = ch.overwrites_for(ctx.guild.default_role)
        ow.send_messages = None
        await ch.set_permissions(ctx.guild.default_role, overwrite=ow, reason=f"Unlock da {ctx.author}")
        await ctx.send(f"🔓 {ch.mention} sbloccato.")

    @commands.hybrid_group(name="nickname", invoke_without_command=True)
    async def nickname(self, ctx):
        """Gestione nickname."""
        await ctx.send("Usa `nickname set <membro> <nickname>`.")

    @nickname.command(name="set")
    @require(("manage_nicknames",))
    @commands.bot_has_permissions(manage_nicknames=True)
    async def nickname_set(self, ctx, member: discord.Member, *, nickname: str):
        """Cambia il nickname di un membro (scrivi 'reset' per rimuoverlo)."""
        if err := can_act(ctx, member, allow_self=True):
            return await fail(ctx, err)
        await member.edit(nick=None if nickname.lower() == "reset" else nickname[:32])
        await ctx.send(f"✏️ Nickname di {member.mention} aggiornato.")

    @commands.hybrid_command(name="pex")
    @require(("manage_roles",))
    @commands.bot_has_permissions(manage_roles=True)
    async def pex(self, ctx, member: discord.Member, role: discord.Role):
        """Assegna un ruolo a un membro."""
        if err := role_err(ctx, role):
            return await fail(ctx, err)
        if role in member.roles:
            return await fail(ctx, "L'utente ha già questo ruolo.")
        await member.add_roles(role, reason=f"pex da {ctx.author}")
        await mod_log(ctx.guild, member, "pex", ctx.author, f"+{role.name}")
        await ctx.send(embed=emb("✅ Ruolo assegnato", f"{role.mention} → {member.mention}"))

    @commands.hybrid_command(name="depex")
    @require(("manage_roles",))
    @commands.bot_has_permissions(manage_roles=True)
    async def depex(self, ctx, member: discord.Member, role: discord.Role):
        """Rimuove un ruolo da un membro."""
        if err := role_err(ctx, role):
            return await fail(ctx, err)
        if role not in member.roles:
            return await fail(ctx, "L'utente non ha questo ruolo.")
        await member.remove_roles(role, reason=f"depex da {ctx.author}")
        await mod_log(ctx.guild, member, "depex", ctx.author, f"-{role.name}")
        await ctx.send(embed=emb("✅ Ruolo rimosso", f"{role.mention} ← {member.mention}"))

    @commands.hybrid_group(name="modlogs", invoke_without_command=True)
    async def modlogs(self, ctx):
        """Storico delle azioni di moderazione."""
        await ctx.send("Usa `modlogs user <utente>`.")

    @modlogs.command(name="user")
    @require((), ("view_modlogs",))
    async def modlogs_user(self, ctx, user: discord.User):
        """Mostra lo storico di moderazione di un utente."""
        logs = gd(ctx.guild)["modlogs"].get(str(user.id), [])
        if not logs:
            return await ctx.send("Nessuna azione registrata.", ephemeral=True)
        lines = [f"{tstamp(x['ts'], 'd')} · **{x['action']}** · <@{x['mod']}> · {x['reason']}" for x in logs[-15:]]
        await ctx.send(embed=emb(f"🛡️ Modlogs di {user}", "\n".join(lines)), ephemeral=True)


# ============================================================================
#  2. BENVENUTO E INFORMAZIONI
# ============================================================================
def fmt_msg(text, member):
    return (text.replace("{user}", member.mention).replace("{username}", member.name)
            .replace("{server}", member.guild.name).replace("{count}", str(member.guild.member_count)))


class Welcome(commands.Cog, name="Benvenuto e info"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member):
        d = gd(member.guild)
        d["stats"]["joins"] += 1
        store.touch()
        for rid in d["autorole"]:
            r = member.guild.get_role(rid)
            if r and r < member.guild.me.top_role:
                try:
                    await member.add_roles(r, reason="Autorole")
                except discord.HTTPException:
                    pass
        w = d["welcome"]
        ch = member.guild.get_channel(w.get("channel", 0)) if w else None
        if ch:
            try:
                await ch.send(embed=emb("👋 Benvenuto!", fmt_msg(w["message"], member)))
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        d = gd(member.guild)
        d["stats"]["leaves"] += 1
        store.touch()
        g = d["goodbye"]
        ch = member.guild.get_channel(g.get("channel", 0)) if g else None
        if ch:
            try:
                await ch.send(embed=emb("👋 Arrivederci", fmt_msg(g["message"], member)))
            except discord.HTTPException:
                pass

    @commands.hybrid_group(name="welcome", invoke_without_command=True)
    async def welcome(self, ctx):
        """Messaggio di benvenuto: setup, edit, test."""
        await ctx.send("Usa `welcome setup`, `welcome edit` o `welcome test`. Segnaposto: {user} {username} {server} {count}")

    @welcome.command(name="setup")
    @require(("manage_guild",))
    async def welcome_setup(self, ctx, canale: discord.TextChannel, *, messaggio: str = "Benvenuto {user} in **{server}**! Sei il membro #{count}."):
        """Configura il messaggio di benvenuto automatico."""
        gd(ctx.guild)["welcome"] = {"channel": canale.id, "message": messaggio}
        store.touch()
        await ctx.send(f"✅ Benvenuto configurato in {canale.mention}.")

    @welcome.command(name="edit")
    @require(("manage_guild",))
    async def welcome_edit(self, ctx, *, messaggio: str):
        """Modifica il messaggio di benvenuto."""
        w = gd(ctx.guild)["welcome"]
        if not w:
            return await fail(ctx, "Usa prima `welcome setup`.")
        w["message"] = messaggio
        store.touch()
        await ctx.send("✅ Messaggio aggiornato.")

    @welcome.command(name="test")
    @require(("manage_guild",))
    async def welcome_test(self, ctx):
        """Simula un messaggio di benvenuto."""
        w = gd(ctx.guild)["welcome"]
        if not w:
            return await fail(ctx, "Usa prima `welcome setup`.")
        await ctx.send(embed=emb("👋 Benvenuto! (test)", fmt_msg(w["message"], ctx.author)))

    @commands.hybrid_group(name="goodbye", invoke_without_command=True)
    async def goodbye(self, ctx):
        """Messaggio di addio."""
        await ctx.send("Usa `goodbye setup <canale> <messaggio>`.")

    @goodbye.command(name="setup")
    @require(("manage_guild",))
    async def goodbye_setup(self, ctx, canale: discord.TextChannel, *, messaggio: str = "**{username}** ha lasciato {server}."):
        """Configura il messaggio quando un membro lascia il server."""
        gd(ctx.guild)["goodbye"] = {"channel": canale.id, "message": messaggio}
        store.touch()
        await ctx.send(f"✅ Messaggio di addio configurato in {canale.mention}.")

    @commands.hybrid_group(name="autorole", invoke_without_command=True)
    async def autorole(self, ctx):
        """Ruoli automatici per i nuovi membri."""
        await ctx.send("Usa `autorole setup <ruolo>`, `autorole list`, `autorole remove <ruolo>`.")

    @autorole.command(name="setup")
    @require(("manage_roles",))
    async def autorole_setup(self, ctx, role: discord.Role):
        """Aggiunge un ruolo assegnato automaticamente ai nuovi membri."""
        if err := role_err(ctx, role):
            return await fail(ctx, err)
        ids = gd(ctx.guild)["autorole"]
        if role.id not in ids:
            ids.append(role.id)
            store.touch()
        await ctx.send(f"✅ {role.mention} verrà assegnato ai nuovi membri.")

    @autorole.command(name="remove")
    @require(("manage_roles",))
    async def autorole_remove(self, ctx, role: discord.Role):
        """Toglie un ruolo dall'assegnazione automatica."""
        ids = gd(ctx.guild)["autorole"]
        if role.id in ids:
            ids.remove(role.id)
            store.touch()
        await ctx.send(f"✅ {role.mention} rimosso dagli autorole.")

    @autorole.command(name="list")
    @require(("manage_roles",))
    async def autorole_list(self, ctx):
        """Mostra i ruoli assegnati automaticamente."""
        ids = gd(ctx.guild)["autorole"]
        await ctx.send(embed=emb("🎭 Autorole", "\n".join(f"<@&{i}>" for i in ids) or "Nessun ruolo configurato."), ephemeral=True)

    @commands.hybrid_group(name="server", invoke_without_command=True)
    async def server(self, ctx):
        """Informazioni sul server."""
        await ctx.send("Usa `server info` o `server stats`.")

    @server.command(name="info")
    async def server_info(self, ctx):
        """Mostra informazioni sul server."""
        g = ctx.guild
        e = emb(g.name)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        e.add_field(name="ID", value=g.id)
        e.add_field(name="Proprietario", value=f"<@{g.owner_id}>")
        e.add_field(name="Membri", value=g.member_count)
        e.add_field(name="Creato", value=discord.utils.format_dt(g.created_at, "D"))
        e.add_field(name="Boost", value=g.premium_subscription_count)
        await ctx.send(embed=e)

    @server.command(name="stats")
    async def server_stats(self, ctx):
        """Mostra statistiche del server."""
        g, st = ctx.guild, gd(ctx.guild)["stats"]
        bots = sum(1 for m in g.members if m.bot)
        e = emb(f"📊 Statistiche di {g.name}")
        e.add_field(name="Membri", value=f"{g.member_count} ({g.member_count - bots} utenti, {bots} bot)")
        e.add_field(name="Canali", value=f"{len(g.text_channels)} testo · {len(g.voice_channels)} vocali")
        e.add_field(name="Ruoli", value=len(g.roles))
        e.add_field(name="Ingressi / Uscite", value=f"{st['joins']} / {st['leaves']} (dal setup del bot)")
        e.add_field(name="Messaggi contati", value=st["messages"])
        await ctx.send(embed=e)

    @commands.hybrid_group(name="user", invoke_without_command=True)
    async def user(self, ctx):
        """Informazioni su un utente."""
        await ctx.send("Usa `user info [membro]`.")

    @user.command(name="info")
    async def user_info(self, ctx, member: Optional[discord.Member] = None):
        """Mostra informazioni su un utente."""
        m = member or ctx.author
        e = emb(str(m))
        e.set_thumbnail(url=m.display_avatar.url)
        e.add_field(name="ID", value=m.id)
        e.add_field(name="Account creato", value=discord.utils.format_dt(m.created_at, "D"))
        e.add_field(name="Entrato", value=discord.utils.format_dt(m.joined_at, "D") if m.joined_at else "?")
        e.add_field(name="Ruolo più alto", value=m.top_role.mention)
        e.add_field(name="Ruoli", value=len(m.roles) - 1)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="membercount")
    async def membercount(self, ctx):
        """Mostra il numero di membri del server."""
        await ctx.send(f"👥 **{ctx.guild.member_count}** membri in {ctx.guild.name}.")

    @commands.hybrid_group(name="announce", invoke_without_command=True)
    async def announce(self, ctx):
        """Annunci."""
        await ctx.send("Usa `announce send <canale> <messaggio>`.")

    @announce.command(name="send")
    @require(("manage_messages",), ("manage_announcements",))
    async def announce_send(self, ctx, canale: discord.TextChannel, *, messaggio: str):
        """Invia un annuncio in un canale selezionato."""
        if not canale.permissions_for(ctx.guild.me).send_messages:
            return await fail(ctx, "Non posso scrivere in quel canale.")
        await canale.send(embed=emb("📢 Annuncio", messaggio))
        await ctx.send("✅ Annuncio inviato.", ephemeral=True)


# ============================================================================
#  3. SICUREZZA E VERIFICA
# ============================================================================
URL_RE = re.compile(r"(?:https?://|www\.|discord\.gg/|discord(?:app)?\.com/invite/)\S+", re.I)


def link_blocked(text, whitelist):
    for u in URL_RE.findall(text):
        host = re.sub(r"^(?:https?://)?(?:www\.)?", "", u.lower()).split("/")[0]
        if not any(host == w or host.endswith("." + w) for w in whitelist):
            return True
    return False


def verify_embed(v):
    return emb("✅ Verifica", v["text"])


def verify_view():
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Verificati", emoji="✅", style=discord.ButtonStyle.success, custom_id="verify:btn"))
    return view


class Security(commands.Cog, name="Sicurezza"):
    def __init__(self, bot):
        self.bot = bot
        self.msgs = {}
        self.joins = {}

    @commands.Cog.listener()
    async def on_member_join(self, member):
        d = gd(member.guild)
        ar = d["antiraid"]
        now = time.time()
        dq = self.joins.setdefault(member.guild.id, collections.deque())
        dq.append(now)
        while dq and now - dq[0] > ar["seconds"]:
            dq.popleft()
        if ar["enabled"] and not d["raidmode"] and len(dq) >= ar["joins"]:
            d["raidmode"] = True
            store.touch()
            await send_log(member.guild, emb("🚨 Raid rilevato", f"{len(dq)} ingressi in {ar['seconds']}s: **raidmode attivata**.", 0xED4245))
        if d["raidmode"] and not member.bot:
            try:
                await member.timeout(discord.utils.utcnow() + dt.timedelta(minutes=30), reason="Raidmode")
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return
        if has_perm(message.author, ("manage_messages",)):
            return
        d = gd(message.guild)
        if d["antilink"]["enabled"] and link_blocked(message.content, d["antilink"]["whitelist"]):
            try:
                await message.delete()
                await message.channel.send(f"🔗 {message.author.mention} i link non sono consentiti qui.", delete_after=6)
            except discord.HTTPException:
                pass
            return
        sp = d["antispam"]
        if sp["enabled"]:
            now = time.time()
            dq = self.msgs.setdefault((message.guild.id, message.author.id), collections.deque())
            dq.append(now)
            while dq and now - dq[0] > sp["seconds"]:
                dq.popleft()
            if len(dq) >= sp["limit"]:
                dq.clear()
                try:
                    await message.author.timeout(discord.utils.utcnow() + dt.timedelta(minutes=5), reason="Antispam")
                    await message.channel.send(f"🚫 {message.author.mention} stai scrivendo troppo in fretta: timeout di 5 minuti.", delete_after=8)
                except discord.HTTPException:
                    pass
                await send_log(message.guild, emb("🚫 Antispam", f"{message.author.mention} in timeout (5m) in {message.channel.mention}", 0xFEE75C))

    @commands.hybrid_command(name="antispam")
    @require(("manage_guild",))
    async def antispam(self, ctx, attivo: bool, messaggi: commands.Range[int, 3, 20] = 5, secondi: commands.Range[int, 2, 60] = 6):
        """Attiva/configura la protezione contro lo spam."""
        gd(ctx.guild)["antispam"] = {"enabled": attivo, "limit": messaggi, "seconds": secondi}
        store.touch()
        await ctx.send(f"🛡️ Antispam **{'attivo' if attivo else 'disattivo'}** ({messaggi} messaggi in {secondi}s → timeout 5m).")

    @commands.hybrid_command(name="antilink")
    @require(("manage_guild",))
    async def antilink(self, ctx, attivo: bool, *, domini_consentiti: Optional[str] = None):
        """Attiva/configura il blocco dei link (domini consentiti separati da virgola)."""
        wl = [re.sub(r"^(https?://)?(www\.)?", "", x.lower()).strip("/") for x in re.split(r"[,\s]+", domini_consentiti or "") if x]
        gd(ctx.guild)["antilink"] = {"enabled": attivo, "whitelist": wl}
        store.touch()
        await ctx.send(f"🔗 Antilink **{'attivo' if attivo else 'disattivo'}**. Domini consentiti: {', '.join(wl) or 'nessuno'}")

    @commands.hybrid_command(name="antiraid")
    @require(("manage_guild",))
    async def antiraid(self, ctx, attivo: bool, ingressi: commands.Range[int, 3, 50] = 6, secondi: commands.Range[int, 3, 120] = 10):
        """Attiva/configura la protezione contro i raid."""
        gd(ctx.guild)["antiraid"] = {"enabled": attivo, "joins": ingressi, "seconds": secondi}
        store.touch()
        await ctx.send(f"🛡️ Antiraid **{'attivo' if attivo else 'disattivo'}** ({ingressi} ingressi in {secondi}s → raidmode).")

    @commands.hybrid_group(name="raidmode", invoke_without_command=True)
    async def raidmode(self, ctx):
        """Modalità di sicurezza rafforzata."""
        await ctx.send("Usa `raidmode enable` o `raidmode disable`.")

    @raidmode.command(name="enable")
    @require(("manage_guild",))
    async def raidmode_enable(self, ctx):
        """Attiva la raidmode (i nuovi membri vanno in timeout 30m)."""
        gd(ctx.guild)["raidmode"] = True
        store.touch()
        await ctx.send("🚨 **Raidmode attiva**: i nuovi membri verranno messi in timeout per 30 minuti.")

    @raidmode.command(name="disable")
    @require(("manage_guild",))
    async def raidmode_disable(self, ctx):
        """Disattiva la raidmode."""
        gd(ctx.guild)["raidmode"] = False
        store.touch()
        await ctx.send("✅ Raidmode disattivata.")

    @commands.hybrid_group(name="security", invoke_without_command=True)
    async def security(self, ctx):
        """Stato delle protezioni."""
        await ctx.send("Usa `security status`.")

    @security.command(name="status")
    async def security_status(self, ctx):
        """Mostra lo stato delle protezioni configurate."""
        d = gd(ctx.guild)
        onoff = lambda b: "🟢 attivo" if b else "🔴 disattivo"
        e = emb("🛡️ Stato sicurezza")
        e.add_field(name="Antispam", value=onoff(d["antispam"]["enabled"]))
        e.add_field(name="Antilink", value=onoff(d["antilink"]["enabled"]))
        e.add_field(name="Antiraid", value=onoff(d["antiraid"]["enabled"]))
        e.add_field(name="Raidmode", value=onoff(d["raidmode"]))
        e.add_field(name="Verifica", value=onoff(d["verification"]["enabled"]))
        e.add_field(name="AutoMod", value=onoff(d["automod"]["enabled"]))
        if has_perm(ctx.author, ("manage_guild",)):
            e.add_field(name="Dettagli", inline=False, value=(
                f"Antispam: {d['antispam']['limit']} msg/{d['antispam']['seconds']}s\n"
                f"Antiraid: {d['antiraid']['joins']} ingressi/{d['antiraid']['seconds']}s\n"
                f"Domini consentiti: {', '.join(d['antilink']['whitelist']) or 'nessuno'}"))
        await ctx.send(embed=e)

    @commands.hybrid_group(name="verification", invoke_without_command=True)
    async def verification(self, ctx):
        """Sistema di verifica."""
        await ctx.send("Usa `verification setup/edit/enable/disable`.")

    @verification.command(name="setup")
    @require(("manage_guild",))
    async def verification_setup(self, ctx, canale: discord.TextChannel, ruolo: discord.Role):
        """Configura il sistema di verifica dei nuovi membri."""
        if err := role_err(ctx, ruolo):
            return await fail(ctx, err)
        v = gd(ctx.guild)["verification"]
        v.update(enabled=True, role=ruolo.id, channel=canale.id)
        msg = await canale.send(embed=verify_embed(v), view=verify_view())
        v["message_id"] = msg.id
        store.touch()
        await ctx.send(f"✅ Verifica attiva in {canale.mention} (ruolo {ruolo.mention}).")

    @verification.command(name="edit")
    @require(("manage_guild",))
    async def verification_edit(self, ctx, ruolo: Optional[discord.Role] = None, *, testo: Optional[str] = None):
        """Modifica ruolo e/o testo della verifica."""
        v = gd(ctx.guild)["verification"]
        if not v["channel"]:
            return await fail(ctx, "Usa prima `verification setup`.")
        if ruolo:
            if err := role_err(ctx, ruolo):
                return await fail(ctx, err)
            v["role"] = ruolo.id
        if testo:
            v["text"] = testo
        store.touch()
        ch = ctx.guild.get_channel(v["channel"])
        try:
            msg = await ch.fetch_message(v["message_id"])
            await msg.edit(embed=verify_embed(v), view=verify_view())
        except (discord.HTTPException, AttributeError):
            pass
        await ctx.send("✅ Verifica aggiornata.")

    @verification.command(name="enable")
    @require(("manage_guild",))
    async def verification_enable(self, ctx):
        """Attiva il sistema di verifica."""
        v = gd(ctx.guild)["verification"]
        if not v["role"]:
            return await fail(ctx, "Usa prima `verification setup`.")
        v["enabled"] = True
        store.touch()
        await ctx.send("✅ Verifica attivata.")

    @verification.command(name="disable")
    @require(("manage_guild",))
    async def verification_disable(self, ctx):
        """Disattiva il sistema di verifica."""
        gd(ctx.guild)["verification"]["enabled"] = False
        store.touch()
        await ctx.send("✅ Verifica disattivata.")


# ============================================================================
#  4. TICKET (comandi)
# ============================================================================
class Tickets(commands.Cog, name="Ticket"):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="ticketpanelcreate")
    @require(("manage_channels",), ("manage_tickets",))
    async def ticketpanelcreate(self, ctx, canale: Optional[discord.TextChannel] = None, *, nome: Optional[str] = None):
        """Crea e pubblica un pannello ticket."""
        ch = canale or ctx.channel
        if not isinstance(ch, discord.TextChannel):
            return await fail(ctx, "Scegli un canale di testo.")
        p = new_panel(nome or "Assistenza")
        err = await publish_panel(ctx.guild, p, ch)
        if err:
            return await fail(ctx, err)
        gd(ctx.guild)["panels"][p["id"]] = p
        store.touch()
        await ctx.send(content=f"✅ Pannello **{p['name']}** creato e pubblicato in {ch.mention}. Puoi modificarlo qui sotto.",
                       embed=panel_summary(ctx.guild, p), view=PanelEditor(ctx.author.id, p["id"]), ephemeral=True)

    @commands.hybrid_command(name="ticketpaneledit")
    @require(("manage_channels",), ("manage_tickets",))
    async def ticketpaneledit(self, ctx):
        """Modifica un pannello ticket esistente."""
        panels = gd(ctx.guild)["panels"]
        if not panels:
            return await fail(ctx, "Non ci sono pannelli: creane uno con `ticketpanelcreate`.")
        opts = [discord.SelectOption(label=p["name"][:100], value=pid,
                                     description=f"{len(p['categories'])} categorie") for pid, p in list(panels.items())[:25]]
        view = discord.ui.View(timeout=300)
        sel = discord.ui.Select(placeholder="Scegli un pannello", options=opts)

        async def cb(i):
            if i.user.id != ctx.author.id:
                return await i.response.send_message("Non è il tuo menu.", ephemeral=True)
            await show_editor(i, ctx.author.id, sel.values[0])
        sel.callback = cb
        view.add_item(sel)
        await ctx.send("Quale pannello vuoi modificare?", view=view, ephemeral=True)

    @commands.hybrid_command(name="ticketpanellist")
    @require(("manage_channels",), ("manage_tickets",))
    async def ticketpanellist(self, ctx):
        """Mostra i pannelli ticket configurati nel server."""
        panels = gd(ctx.guild)["panels"]
        lines = [f"**{p['name']}** · {len(p['categories'])} categorie · "
                 + (f"<#{p['channel_id']}>" if p["channel_id"] else "non pubblicato") for p in panels.values()]
        await ctx.send(embed=emb("🎫 Pannelli ticket", "\n".join(lines) or "Nessun pannello."), ephemeral=True)

    @commands.hybrid_command(name="ticketpaneldelete")
    @require(("manage_channels",), ("manage_tickets",))
    async def ticketpaneldelete(self, ctx, *, nome: str):
        """Elimina un pannello ticket (per nome)."""
        panels = gd(ctx.guild)["panels"]
        pid = next((k for k, p in panels.items() if p["name"].lower() == nome.lower()), None)
        if not pid:
            return await fail(ctx, "Pannello non trovato.")
        p = panels.pop(pid)
        store.touch()
        ch = ctx.guild.get_channel(p["channel_id"]) if p["channel_id"] else None
        if ch:
            try:
                await (await ch.fetch_message(p["message_id"])).delete()
            except discord.HTTPException:
                pass
        await ctx.send(f"🗑️ Pannello **{p['name']}** eliminato.")

    @commands.hybrid_group(name="ticket", invoke_without_command=True)
    async def ticket(self, ctx):
        """Statistiche ticket."""
        await ctx.send("Usa `ticket stats`.")

    @ticket.command(name="stats")
    @require(("manage_channels",), ("manage_tickets",))
    async def ticket_stats(self, ctx):
        """Mostra statistiche dei ticket (aperti e chiusi)."""
        d = gd(ctx.guild)
        open_ = [t for t in d["tickets"].values() if t["status"] == "open"]
        e = emb("🎫 Statistiche ticket")
        e.add_field(name="Aperti", value=len(open_))
        e.add_field(name="Chiusi (eliminati)", value=d["ticket_stats"]["closed"])
        e.add_field(name="Totali creati", value=d["ticket_counter"])
        e.add_field(name="Presi in carico", value=sum(1 for t in open_ if t["claimed"]))
        await ctx.send(embed=e, ephemeral=True)


# ============================================================================
#  5. PARTNERSHIP
# ============================================================================
class Partnership(commands.Cog, name="Partnership"):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="partnership", invoke_without_command=True)
    async def partnership(self, ctx):
        """Gestione partnership."""
        await ctx.send("Sottocomandi: create, delete, info, list, setup, requirements, leaderboard, channel set/view.")

    @partnership.command(name="create")
    @require((), ("manage_partnerships",))
    async def p_create(self, ctx, rappresentante: discord.Member, link: str, *, nome: str):
        """Registra una nuova partnership."""
        d = gd(ctx.guild)
        cfg = d["partner_cfg"]
        pid = cfg["next"]
        cfg["next"] += 1
        rec = {"id": pid, "name": nome, "link": link, "rep": rappresentante.id, "by": ctx.author.id, "ts": int(time.time())}
        d["partners"][str(pid)] = rec
        store.touch()
        role = ctx.guild.get_role(cfg["role"]) if cfg["role"] else None
        if role and role < ctx.guild.me.top_role:
            try:
                await rappresentante.add_roles(role, reason="Partnership")
            except discord.HTTPException:
                pass
        ch = ctx.guild.get_channel(cfg["channel"]) if cfg["channel"] else None
        if ch:
            e = emb(f"🤝 Nuova partnership: {nome}", f"{link}\nRappresentante: {rappresentante.mention}\nGestita da: {ctx.author.mention}")
            try:
                await ch.send(embed=e)
            except discord.HTTPException:
                pass
        await ctx.send(f"✅ Partnership **#{pid}** registrata ({nome}).")

    @partnership.command(name="delete")
    @require((), ("manage_partnerships",))
    async def p_delete(self, ctx, id: int):
        """Elimina una partnership registrata."""
        if gd(ctx.guild)["partners"].pop(str(id), None) is None:
            return await fail(ctx, "Partnership non trovata.")
        store.touch()
        await ctx.send(f"🗑️ Partnership #{id} eliminata.")

    @partnership.command(name="info")
    async def p_info(self, ctx, id: int):
        """Mostra i dettagli di una partnership."""
        r = gd(ctx.guild)["partners"].get(str(id))
        if not r:
            return await fail(ctx, "Partnership non trovata.")
        e = emb(f"🤝 {r['name']}", r["link"])
        e.add_field(name="Registrata", value=tstamp(r["ts"], "D"))
        if has_perm(ctx.author, (), ("manage_partnerships",)):
            e.add_field(name="Rappresentante", value=f"<@{r['rep']}>")
            e.add_field(name="Gestita da", value=f"<@{r['by']}>")
        await ctx.send(embed=e)

    @partnership.command(name="list")
    async def p_list(self, ctx):
        """Elenca le partnership registrate."""
        full = has_perm(ctx.author, (), ("manage_partnerships",))
        lines = [f"**#{r['id']}** {r['name']} — {r['link']}" + (f" · <@{r['by']}>" if full else "")
                 for r in gd(ctx.guild)["partners"].values()]
        await ctx.send(embed=emb("🤝 Partnership", cut("\n".join(lines) or "Nessuna partnership.", 4000)))

    @partnership.command(name="setup")
    @require(("manage_guild",), ("manage_partnerships",))
    async def p_setup(self, ctx, canale: discord.TextChannel, ruolo: Optional[discord.Role] = None):
        """Configura il sistema partnership (canale e ruolo partner opzionale)."""
        cfg = gd(ctx.guild)["partner_cfg"]
        cfg["channel"] = canale.id
        cfg["role"] = ruolo.id if ruolo else None
        store.touch()
        await ctx.send(f"✅ Partnership configurate: canale {canale.mention}" + (f", ruolo {ruolo.mention}" if ruolo else "") + ".")

    @partnership.command(name="requirements")
    async def p_req(self, ctx, *, testo: Optional[str] = None):
        """Mostra o modifica i requisiti per ottenere una partnership."""
        cfg = gd(ctx.guild)["partner_cfg"]
        if testo is None:
            return await ctx.send(embed=emb("📋 Requisiti partnership", cfg["requirements"]))
        if not has_perm(ctx.author, (), ("manage_partnerships",)):
            raise NoPerm(["Manage Partnerships"])
        cfg["requirements"] = testo
        store.touch()
        await ctx.send("✅ Requisiti aggiornati.")

    @partnership.command(name="leaderboard")
    async def p_lb(self, ctx):
        """Mostra la classifica delle partnership per gestore."""
        c = collections.Counter(str(r["by"]) for r in gd(ctx.guild)["partners"].values())
        await ctx.send(embed=emb("🏆 Classifica partnership", top_lines(ctx.guild, c.most_common(), lambda v: f"{v} partnership")))

    @partnership.group(name="channel", invoke_without_command=True)
    async def p_channel(self, ctx):
        """Canale delle partnership."""
        ch = gd(ctx.guild)["partner_cfg"]["channel"]
        await ctx.send(f"Canale partnership: <#{ch}>" if ch else "Nessun canale impostato.")

    @p_channel.command(name="set")
    @require(("manage_channels",), ("manage_partnerships",))
    async def p_channel_set(self, ctx, canale: discord.TextChannel):
        """Imposta il canale delle partnership."""
        gd(ctx.guild)["partner_cfg"]["channel"] = canale.id
        store.touch()
        await ctx.send(f"✅ Canale partnership: {canale.mention}")

    @p_channel.command(name="view")
    async def p_channel_view(self, ctx):
        """Mostra il canale partnership configurato."""
        ch = gd(ctx.guild)["partner_cfg"]["channel"]
        await ctx.send(f"Canale partnership: <#{ch}>" if ch else "Nessun canale impostato.")


# ============================================================================
#  6. ECONOMIA
# ============================================================================
LOOT = ["🍀 Quadrifoglio", "💎 Gemma", "📦 Scatola misteriosa", "🔑 Chiave antica", "🧪 Pozione"]
JOBS = ["hai consegnato pacchi", "hai riparato dei computer", "hai servito ai tavoli", "hai programmato un bot",
        "hai giocato a un torneo", "hai fatto il moderatore per un giorno"]


def eco(d, uid):
    return d["economy"].setdefault(str(uid), {"wallet": 0, "bank": 0, "daily": 0, "work": 0, "items": {}})


def parse_amount(text, avail):
    t = text.strip().lower()
    if t in ("all", "tutto", "max"):
        return avail
    return int(t) if t.isdigit() else None


class Economy(commands.Cog, name="Economia"):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="balance")
    async def balance(self, ctx, member: Optional[discord.Member] = None):
        """Mostra il saldo virtuale di un utente."""
        m = member or ctx.author
        u = eco(gd(ctx.guild), m.id)
        await ctx.send(embed=emb(f"{CURRENCY} Saldo di {m.display_name}",
                                 f"Portafoglio: **{u['wallet']}**\nBanca: **{u['bank']}**\nTotale: **{u['wallet'] + u['bank']}**"))

    @commands.hybrid_command(name="daily")
    async def daily(self, ctx):
        """Raccoglie la ricompensa giornaliera."""
        u = eco(gd(ctx.guild), ctx.author.id)
        now = time.time()
        if now - u["daily"] < 86400:
            return await fail(ctx, f"Hai già riscosso: riprova {tstamp(u['daily'] + 86400)}.")
        u["daily"] = now
        u["wallet"] += 500
        store.touch()
        await ctx.send(f"🎁 Hai riscosso **500** {CURRENCY}!")

    @commands.hybrid_command(name="work")
    async def work(self, ctx):
        """Svolgi un'attività virtuale per guadagnare valuta."""
        u = eco(gd(ctx.guild), ctx.author.id)
        now = time.time()
        if now - u["work"] < 3600:
            return await fail(ctx, f"Sei stanco: riprova {tstamp(u['work'] + 3600)}.")
        u["work"] = now
        gain = random.randint(50, 250)
        u["wallet"] += gain
        extra = ""
        if random.random() < 0.15:
            item = random.choice(LOOT)
            u["items"][item] = u["items"].get(item, 0) + 1
            extra = f"\nHai trovato: **{item}**!"
        store.touch()
        await ctx.send(f"💼 {random.choice(JOBS).capitalize()} e hai guadagnato **{gain}** {CURRENCY}.{extra}")

    @commands.hybrid_command(name="deposit")
    async def deposit(self, ctx, importo: str):
        """Deposita valuta nel conto bancario (numero o 'all')."""
        u = eco(gd(ctx.guild), ctx.author.id)
        n = parse_amount(importo, u["wallet"])
        if not n or n <= 0 or n > u["wallet"]:
            return await fail(ctx, "Importo non valido o superiore al tuo portafoglio.")
        u["wallet"] -= n
        u["bank"] += n
        store.touch()
        await ctx.send(f"🏦 Depositati **{n}** {CURRENCY}.")

    @commands.hybrid_command(name="withdraw")
    async def withdraw(self, ctx, importo: str):
        """Preleva valuta dal conto bancario (numero o 'all')."""
        u = eco(gd(ctx.guild), ctx.author.id)
        n = parse_amount(importo, u["bank"])
        if not n or n <= 0 or n > u["bank"]:
            return await fail(ctx, "Importo non valido o superiore al tuo saldo in banca.")
        u["bank"] -= n
        u["wallet"] += n
        store.touch()
        await ctx.send(f"💸 Prelevati **{n}** {CURRENCY}.")

    @commands.hybrid_command(name="pay")
    async def pay(self, ctx, member: discord.Member, importo: commands.Range[int, 1, 1_000_000]):
        """Trasferisce valuta a un altro membro (max 1.000.000 per volta)."""
        if member.id == ctx.author.id or member.bot:
            return await fail(ctx, "Destinatario non valido.")
        d = gd(ctx.guild)
        a, b = eco(d, ctx.author.id), eco(d, member.id)
        if a["wallet"] < importo:
            return await fail(ctx, "Non hai abbastanza monete nel portafoglio.")
        a["wallet"] -= importo
        b["wallet"] += importo
        store.touch()
        await ctx.send(f"✅ Hai inviato **{importo}** {CURRENCY} a {member.mention}.")

    @commands.hybrid_command(name="inventory")
    async def inventory(self, ctx):
        """Mostra gli oggetti virtuali posseduti."""
        items = eco(gd(ctx.guild), ctx.author.id)["items"]
        txt = "\n".join(f"{k} × {v}" for k, v in items.items()) or "Inventario vuoto. Prova con `/work`!"
        await ctx.send(embed=emb("🎒 Inventario", txt), ephemeral=True)

    @commands.hybrid_group(name="economy", invoke_without_command=True)
    async def economy(self, ctx):
        """Classifica e gestione economia."""
        await ctx.send("Usa `economy leaderboard` (admin: `economy add/remove`).")

    @economy.command(name="leaderboard")
    async def eco_lb(self, ctx):
        """Mostra la classifica economica dei membri."""
        items = sorted(((k, v["wallet"] + v["bank"]) for k, v in gd(ctx.guild)["economy"].items()), key=lambda x: -x[1])
        await ctx.send(embed=emb(f"{CURRENCY} Classifica economica", top_lines(ctx.guild, items, lambda v: f"{v} {CURRENCY}")))

    @economy.command(name="add")
    @require(("administrator",))
    async def eco_add(self, ctx, member: discord.Member, importo: commands.Range[int, 1, 10_000_000]):
        """(Admin) Aggiunge monete a un membro."""
        eco(gd(ctx.guild), member.id)["wallet"] += importo
        store.touch()
        await ctx.send(f"✅ Aggiunte **{importo}** {CURRENCY} a {member.mention}.")

    @economy.command(name="remove")
    @require(("administrator",))
    async def eco_remove(self, ctx, member: discord.Member, importo: commands.Range[int, 1, 10_000_000]):
        """(Admin) Rimuove monete a un membro."""
        u = eco(gd(ctx.guild), member.id)
        u["wallet"] = max(0, u["wallet"] - importo)
        store.touch()
        await ctx.send(f"✅ Rimosse **{importo}** {CURRENCY} a {member.mention}.")


# ============================================================================
#  7. LIVELLI ED ESPERIENZA
# ============================================================================
def xp_needed(level):
    return 5 * level ** 2 + 50 * level + 100


def level_info(xp):
    lvl = 0
    while xp >= xp_needed(lvl):
        xp -= xp_needed(lvl)
        lvl += 1
    return lvl, xp, xp_needed(lvl)


class Levels(commands.Cog, name="Livelli"):
    def __init__(self, bot):
        self.bot = bot
        self.cool = {}

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or len(message.content) < 3:
            return
        key = (message.guild.id, message.author.id)
        now = time.time()
        if now - self.cool.get(key, 0) < 60:
            return
        self.cool[key] = now
        d = gd(message.guild)
        u = d["levels"].setdefault(str(message.author.id), {"xp": 0})
        before = level_info(u["xp"])[0]
        u["xp"] += random.randint(15, 25)
        store.touch()
        after = level_info(u["xp"])[0]
        if after > before:
            try:
                await message.channel.send(f"🎉 {message.author.mention} è salito al **livello {after}**!")
            except discord.HTTPException:
                pass

    @commands.hybrid_command(name="rank")
    async def rank(self, ctx, member: Optional[discord.Member] = None):
        """Mostra il livello e l'esperienza di un membro."""
        m = member or ctx.author
        lv = gd(ctx.guild)["levels"]
        xp = lv.get(str(m.id), {"xp": 0})["xp"]
        lvl, prog, need = level_info(xp)
        pos = 1 + sum(1 for v in lv.values() if v["xp"] > xp)
        bar = "█" * int(10 * prog / need) + "░" * (10 - int(10 * prog / need))
        await ctx.send(embed=emb(f"📈 {m.display_name}", f"Livello **{lvl}** · Posizione **#{pos}**\n`{bar}` {prog}/{need} XP\nXP totali: {xp}"))

    @commands.hybrid_group(name="level", invoke_without_command=True)
    async def level(self, ctx):
        """Classifica livelli."""
        await ctx.send("Usa `level leaderboard`.")

    @level.command(name="leaderboard")
    async def level_lb(self, ctx):
        """Mostra la classifica dei livelli."""
        items = sorted(((k, v["xp"]) for k, v in gd(ctx.guild)["levels"].items()), key=lambda x: -x[1])
        await ctx.send(embed=emb("🏆 Classifica livelli", top_lines(ctx.guild, items, lambda v: f"liv. {level_info(v)[0]} ({v} XP)")))

    @commands.hybrid_group(name="xp", invoke_without_command=True)
    async def xp(self, ctx):
        """Gestione XP (staff)."""
        await ctx.send("Usa `xp add/remove <membro> <quantità>`.")

    @xp.command(name="add")
    @require((), ("manage_xp",))
    async def xp_add(self, ctx, member: discord.Member, quantita: commands.Range[int, 1, 1_000_000]):
        """Aggiunge punti esperienza a un membro."""
        gd(ctx.guild)["levels"].setdefault(str(member.id), {"xp": 0})["xp"] += quantita
        store.touch()
        await ctx.send(f"✅ +{quantita} XP a {member.mention}.")

    @xp.command(name="remove")
    @require((), ("manage_xp",))
    async def xp_remove(self, ctx, member: discord.Member, quantita: commands.Range[int, 1, 1_000_000]):
        """Rimuove punti esperienza a un membro."""
        u = gd(ctx.guild)["levels"].setdefault(str(member.id), {"xp": 0})
        u["xp"] = max(0, u["xp"] - quantita)
        store.touch()
        await ctx.send(f"✅ -{quantita} XP a {member.mention}.")


# ============================================================================
#  8. GIOCHI
# ============================================================================
BALL = ["Sì, certamente.", "Assolutamente no.", "Forse...", "Molto probabile.", "Non ci contare.", "Chiedimelo più tardi.",
        "I segnali dicono di sì.", "Le mie fonti dicono di no.", "Senza dubbio!", "Meglio non dirtelo ora."]
LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


def ttt_winner(b):
    for x, y, z in LINES:
        if b[x] is not None and b[x] == b[y] == b[z]:
            return b[x]
    return None


def ttt_bot_move(b):
    free = [i for i in range(9) if b[i] is None]
    for who in (1, 0):  # prima vinci, poi blocca
        for i in free:
            b[i] = who
            w = ttt_winner(b)
            b[i] = None
            if w == who:
                return i
    return 4 if 4 in free else random.choice(free)


class TTTButton(discord.ui.Button):
    def __init__(self, idx):
        super().__init__(label="\u200b", style=discord.ButtonStyle.secondary, row=idx // 3)
        self.idx = idx

    async def callback(self, interaction):
        await self.view.play(interaction, self.idx)


class TTTView(discord.ui.View):
    def __init__(self, p0, p1, vs_bot):
        super().__init__(timeout=300)
        self.players, self.vs_bot, self.turn = [p0, p1], vs_bot, 0
        self.board = [None] * 9
        for i in range(9):
            self.add_item(TTTButton(i))

    def status(self):
        return f"❌ {self.players[0].mention} vs ⭕ {self.players[1].mention}\nTocca a {self.players[self.turn].mention}"

    def mark(self, idx, who):
        b = self.children[idx]
        b.label, b.disabled = ("X", "O")[who], True
        b.style = discord.ButtonStyle.danger if who == 0 else discord.ButtonStyle.success

    def result(self):
        w = ttt_winner(self.board)
        if w is not None:
            return w
        return -1 if all(x is not None for x in self.board) else None

    async def play(self, i, idx):
        if i.user.id != self.players[self.turn].id:
            return await i.response.send_message("Non è il tuo turno!", ephemeral=True)
        who = self.turn
        self.board[idx] = who
        self.mark(idx, who)
        res = self.result()
        if res is None:
            if self.vs_bot:
                mv = ttt_bot_move(self.board)
                self.board[mv] = 1
                self.mark(mv, 1)
                res = self.result()
            else:
                self.turn = 1 - who
        if res is None:
            text = self.status()
        else:
            for c in self.children:
                c.disabled = True
            self.stop()
            text = "🤝 Pareggio!" if res == -1 else f"🏆 Ha vinto {self.players[res].mention}!"
        await i.response.edit_message(content=text, view=self)


class Games(commands.Cog, name="Giochi"):
    def __init__(self, bot):
        self.bot = bot
        self.active = set()

    @commands.hybrid_command(name="8ball")
    async def eightball(self, ctx, *, domanda: str):
        """Risponde a una domanda con una risposta casuale."""
        await ctx.send(f"🎱 **{cut(domanda, 200)}**\n{random.choice(BALL)}")

    @commands.hybrid_command(name="coinflip")
    async def coinflip(self, ctx):
        """Lancia una moneta virtuale."""
        await ctx.send(f"🪙 {random.choice(['Testa', 'Croce'])}!")

    @commands.hybrid_group(name="guessnumber", invoke_without_command=True)
    async def guessnumber(self, ctx):
        """Gioco: indovina il numero."""
        await ctx.send("Usa `guessnumber start`.")

    @guessnumber.command(name="start")
    async def guess_start(self, ctx):
        """Avvia il gioco: indovina un numero da 1 a 100."""
        key = (ctx.channel.id, ctx.author.id)
        if key in self.active:
            return await fail(ctx, "Hai già una partita in corso qui.")
        self.active.add(key)
        n = random.randint(1, 100)
        try:
            await ctx.send("🔢 Ho scelto un numero da **1 a 100**. Hai 7 tentativi: scrivilo in chat!")
            for left in range(7, 0, -1):
                try:
                    msg = await self.bot.wait_for("message", timeout=45, check=lambda m: m.author.id == ctx.author.id
                                                  and m.channel.id == ctx.channel.id and m.content.strip().isdigit())
                except asyncio.TimeoutError:
                    return await ctx.send(f"⏰ Tempo scaduto! Il numero era **{n}**.")
                g = int(msg.content)
                if g == n:
                    return await ctx.send(f"🎉 Esatto, era **{n}**!")
                await ctx.send(f"{'📈 Più alto' if g < n else '📉 Più basso'}! Tentativi rimasti: {left - 1}")
            await ctx.send(f"😢 Hai perso! Il numero era **{n}**.")
        finally:
            self.active.discard(key)

    @commands.hybrid_command(name="tictactoe")
    async def tictactoe(self, ctx, avversario: Optional[discord.Member] = None):
        """Partita a tris contro un utente o contro il bot."""
        if avversario and (avversario.id == ctx.author.id or avversario.bot):
            return await fail(ctx, "Avversario non valido.")
        vs_bot = avversario is None
        view = TTTView(ctx.author, avversario or ctx.guild.me, vs_bot)
        await ctx.send(view.status(), view=view)

    @commands.hybrid_command(name="ship")
    async def ship(self, ctx, utente1: discord.Member, utente2: Optional[discord.Member] = None):
        """Calcola una compatibilità giocosa tra due utenti."""
        b = utente2 or ctx.author
        pct = (utente1.id ^ b.id) % 101
        bar = "❤️" * (pct // 10) + "🖤" * (10 - pct // 10)
        await ctx.send(f"💘 **{utente1.display_name}** × **{b.display_name}**\n{bar} **{pct}%**")


# ============================================================================
#  9. UTILITÀ
# ============================================================================
async def translate_text(text, lang):
    # endpoint pubblico non ufficiale di Google Translate (nessuna chiave richiesta)
    params = {"client": "gtx", "sl": "auto", "tl": lang, "dt": "t", "q": text}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get("https://translate.googleapis.com/translate_a/single", params=params) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
    return "".join(seg[0] for seg in data[0] if seg and seg[0]), data[2]


class Utility(commands.Cog, name="Utilità"):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="help")
    async def help(self, ctx, categoria: Optional[str] = None):
        """Mostra l'elenco dei comandi e le relative spiegazioni."""
        cats = {}
        for name, cog in self.bot.cogs.items():
            cmds = sorted(c.qualified_name for c in cog.walk_commands() if not getattr(c, "commands", None))
            if cmds:
                cats[name] = cmds
        if categoria:
            match = next((k for k in cats if k.lower().startswith(categoria.lower())), None)
            if not match:
                return await fail(ctx, "Categoria non trovata. Categorie: " + ", ".join(cats))
            e = emb(f"📖 {match}", "\n".join(f"`/{c}` — {self.bot.get_command(c).short_doc or ''}" for c in cats[match]))
            e.description = cut(e.description, 4000)
        else:
            e = emb("📖 Comandi di Star Bot", "Usa `/help <categoria>` per i dettagli. Prefissi: `star` / `Star` / `STAR` e `/`.")
            for k, v in cats.items():
                e.add_field(name=k, value=cut(" ".join(f"`{c}`" for c in v), 1024), inline=False)
        await ctx.send(embed=e, ephemeral=True)

    @commands.hybrid_group(name="bot", invoke_without_command=True)
    async def bot_(self, ctx):
        """Informazioni sul bot."""
        await ctx.send("Usa `bot info` o `bot invite`.")

    @bot_.command(name="info")
    async def bot_info(self, ctx):
        """Mostra informazioni sul bot."""
        app = await self.bot.application_info()
        e = emb("⭐ Star Bot")
        e.add_field(name="Versione", value=f"1.0 · discord.py {discord.__version__}")
        e.add_field(name="Sviluppatore", value=str(app.owner))
        e.add_field(name="Server", value=len(self.bot.guilds))
        e.add_field(name="Latenza", value=f"{round(self.bot.latency * 1000)} ms")
        await ctx.send(embed=e)

    @bot_.command(name="invite")
    async def bot_invite(self, ctx):
        """Fornisce il link per invitare il bot."""
        perms = discord.Permissions(administrator=True)
        url = discord.utils.oauth_url(self.bot.user.id, permissions=perms, scopes=("bot", "applications.commands"))
        await ctx.send(f"🔗 Invita il bot: {url}")

    @commands.hybrid_command(name="language")
    async def language(self, ctx, lingua: Optional[Literal["it", "en"]] = None, server: bool = False):
        """Mostra o cambia la lingua (personale, o predefinita del server)."""
        d = gd(ctx.guild)
        if lingua is None:
            return await ctx.send(f"🌍 Lingua server: **{d['language']}** · tua: **{d['user_lang'].get(str(ctx.author.id), 'predefinita')}**")
        if server:
            if not has_perm(ctx.author, ("manage_guild",)):
                raise NoPerm(["Manage Guild"])
            d["language"] = lingua
        else:
            d["user_lang"][str(ctx.author.id)] = lingua
        store.touch()
        await ctx.send(f"✅ Lingua {'del server' if server else 'personale'} impostata su **{lingua}** (al momento i messaggi del bot sono in italiano).")

    @commands.hybrid_command(name="support")
    async def support(self, ctx):
        """Mostra come contattare il supporto."""
        txt = "Apri un **ticket** dal pannello assistenza del server e lo staff ti risponderà."
        if SUPPORT_URL:
            txt += f"\nServer di supporto: {SUPPORT_URL}"
        await ctx.send(embed=emb("🆘 Supporto", txt))

    @commands.hybrid_command(name="translate")
    async def translate(self, ctx, lingua: str, *, testo: str):
        """Traduce il testo nella lingua scelta (es. en, it, fr)."""
        if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z]{2,4})?", lingua):
            return await fail(ctx, "Codice lingua non valido (es. `en`, `it`, `fr`).")
        if ctx.interaction:
            await ctx.defer()
        try:
            res = await translate_text(testo, lingua.lower())
        except Exception:
            res = None
        if not res:
            return await fail(ctx, "Traduzione non disponibile al momento.")
        await ctx.send(embed=emb(f"🌐 {res[1]} → {lingua}", cut(res[0], 4000)))


class Config(commands.Cog, name="Configurazione"):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="staffperm", invoke_without_command=True)
    async def staffperm(self, ctx):
        """Assegna i permessi interni del bot ai ruoli."""
        await ctx.send("Usa `staffperm add/remove <permesso> <ruolo>` oppure `staffperm list`.")

    @staffperm.command(name="add")
    @require(("administrator",))
    async def sp_add(self, ctx, permesso: Literal[tuple(INTERNAL_PERMS)], ruolo: discord.Role):  # type: ignore
        """Concede un permesso interno a un ruolo."""
        ids = gd(ctx.guild)["staffperms"].setdefault(permesso, [])
        if ruolo.id not in ids:
            ids.append(ruolo.id)
            store.touch()
        await ctx.send(f"✅ {ruolo.mention} ha ora **{INTERNAL_PERMS[permesso]}**.")

    @staffperm.command(name="remove")
    @require(("administrator",))
    async def sp_remove(self, ctx, permesso: Literal[tuple(INTERNAL_PERMS)], ruolo: discord.Role):  # type: ignore
        """Toglie un permesso interno a un ruolo."""
        ids = gd(ctx.guild)["staffperms"].get(permesso, [])
        if ruolo.id in ids:
            ids.remove(ruolo.id)
            store.touch()
        await ctx.send(f"✅ {ruolo.mention} non ha più **{INTERNAL_PERMS[permesso]}**.")

    @staffperm.command(name="list")
    @require(("administrator",))
    async def sp_list(self, ctx):
        """Mostra i permessi interni assegnati."""
        sp = gd(ctx.guild)["staffperms"]
        lines = [f"**{INTERNAL_PERMS[k]}**: " + " ".join(f"<@&{r}>" for r in v) for k, v in sp.items() if v]
        await ctx.send(embed=emb("🔑 Permessi interni", "\n".join(lines) or "Nessun permesso assegnato."), ephemeral=True)


# ============================================================================
#  10. LOG
# ============================================================================
class Logs(commands.Cog, name="Log"):
    def __init__(self, bot):
        self.bot = bot

    def _skip(self, guild, channel_id):
        return gd(guild)["log_channel"] in (None, channel_id)

    @commands.Cog.listener()
    async def on_message_delete(self, m):
        if not m.guild or m.author.bot or self._skip(m.guild, m.channel.id):
            return
        await send_log(m.guild, emb("🗑️ Messaggio eliminato", f"**Autore:** {m.author.mention}\n**Canale:** {m.channel.mention}\n{cut(m.content or '[senza testo]', 1000)}", 0xED4245))

    @commands.Cog.listener()
    async def on_message_edit(self, a, b):
        if not a.guild or a.author.bot or a.content == b.content or self._skip(a.guild, a.channel.id):
            return
        await send_log(a.guild, emb("✏️ Messaggio modificato", f"**Autore:** {a.author.mention} in {a.channel.mention}\n**Prima:** {cut(a.content, 800)}\n**Dopo:** {cut(b.content, 800)}", 0xFEE75C))

    @commands.Cog.listener()
    async def on_member_join(self, m):
        await send_log(m.guild, emb("📥 Membro entrato", f"{m.mention} (`{m.id}`)\nAccount creato {tstamp(m.created_at.timestamp())}", 0x57F287))

    @commands.Cog.listener()
    async def on_member_remove(self, m):
        await send_log(m.guild, emb("📤 Membro uscito", f"{m} (`{m.id}`)", 0xED4245))

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        await send_log(guild, emb("🔨 Ban", f"{user} (`{user.id}`)", 0xED4245))

    @commands.Cog.listener()
    async def on_member_unban(self, guild, user):
        await send_log(guild, emb("♻️ Unban", f"{user} (`{user.id}`)", 0x57F287))

    @commands.Cog.listener()
    async def on_member_update(self, a, b):
        if a.roles != b.roles:
            added, removed = set(b.roles) - set(a.roles), set(a.roles) - set(b.roles)
            txt = " ".join([f"➕{r.mention}" for r in added] + [f"➖{r.mention}" for r in removed])
            await send_log(b.guild, emb("🎭 Ruoli aggiornati", f"{b.mention}: {txt}"))

    @commands.Cog.listener()
    async def on_guild_channel_create(self, ch):
        await send_log(ch.guild, emb("📁 Canale creato", ch.mention, 0x57F287))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, ch):
        await send_log(ch.guild, emb("📁 Canale eliminato", f"#{ch.name}", 0xED4245))

    @commands.hybrid_group(name="logs", invoke_without_command=True)
    async def logs(self, ctx):
        """Registro eventi."""
        await ctx.send("Usa `logs test` o `logs channel set/reset/view`.")

    @logs.command(name="test")
    @require(("manage_guild",), ("manage_logs",))
    async def logs_test(self, ctx):
        """Invia un messaggio di prova nel canale dei log."""
        ch_id = gd(ctx.guild)["log_channel"]
        if not ch_id:
            return await fail(ctx, "Nessun canale log impostato.")
        await send_log(ctx.guild, emb("✅ Test log", f"Messaggio di prova richiesto da {ctx.author.mention}."))
        await ctx.send("✅ Messaggio di prova inviato.", ephemeral=True)

    @logs.group(name="channel", invoke_without_command=True)
    async def logs_channel(self, ctx):
        """Canale dei log."""
        ch = gd(ctx.guild)["log_channel"]
        await ctx.send(f"Canale log: <#{ch}>" if ch else "Nessun canale log impostato.")

    @logs_channel.command(name="view")
    async def logs_channel_view(self, ctx):
        """Mostra il canale dei log."""
        ch = gd(ctx.guild)["log_channel"]
        await ctx.send(f"Canale log: <#{ch}>" if ch else "Nessun canale log impostato.", ephemeral=True)

    @logs_channel.command(name="set")
    @require(("manage_guild",), ("manage_logs",))
    async def logs_channel_set(self, ctx, canale: discord.TextChannel):
        """Imposta il canale in cui inviare i registri."""
        p = canale.permissions_for(ctx.guild.me)
        if not (p.send_messages and p.embed_links):
            return await fail(ctx, "Mi servono i permessi Invia messaggi e Incorpora link in quel canale.")
        gd(ctx.guild)["log_channel"] = canale.id
        store.touch()
        await ctx.send(f"✅ Log nel canale {canale.mention}. Ricorda: limita la visibilità allo staff.")

    @logs_channel.command(name="reset")
    @require(("manage_guild",), ("manage_logs",))
    async def logs_channel_reset(self, ctx):
        """Rimuove la configurazione del canale dei log."""
        gd(ctx.guild)["log_channel"] = None
        store.touch()
        await ctx.send("✅ Canale log rimosso.")


# ============================================================================
#  11. COMANDI PERSONALIZZATI (say / embed)
# ============================================================================
class Custom(commands.Cog, name="Personalizzati"):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="say")
    @require((), ("use_say",))
    async def say(self, ctx, canale: Optional[discord.TextChannel] = None, *, testo: str):
        """Fa inviare al bot un messaggio con il testo scelto."""
        ch = canale or ctx.channel
        if not ch.permissions_for(ctx.guild.me).send_messages:
            return await fail(ctx, "Non posso scrivere in quel canale.")
        await ch.send(testo, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
        if ctx.interaction:
            await ctx.send("✅ Inviato.", ephemeral=True)
        else:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    @commands.hybrid_command(name="embed")
    @require(("manage_messages",), ("manage_embeds",))
    async def embed(self, ctx, titolo: str, descrizione: str, colore: str = "#5865F2", canale: Optional[discord.TextChannel] = None):
        """Crea un embed personalizzato (usa \\n per andare a capo; con il prefisso metti gli spazi tra virgolette)."""
        try:
            col = int(colore.strip("#"), 16)
        except ValueError:
            return await fail(ctx, "Colore non valido (es. #FF0000).")
        ch = canale or ctx.channel
        await ch.send(embed=emb(cut(titolo, 256), cut(descrizione.replace("\\n", "\n"), 4000), col))
        if ctx.interaction:
            await ctx.send("✅ Embed inviato.", ephemeral=True)


# ============================================================================
#  12. STATO DEL BOT
# ============================================================================
STATUSES = {"online": discord.Status.online, "idle": discord.Status.idle, "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible}


async def apply_presence(bot, cfg):
    cfg = cfg or {}
    act = discord.Game(name=cfg["text"]) if cfg.get("text") else discord.Game(name="/help · star help")
    await bot.change_presence(status=STATUSES.get(cfg.get("status", "online"), discord.Status.online), activity=act)


class StatusCog(commands.Cog, name="Stato"):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="status")
    async def status(self, ctx, stato: Optional[Literal["online", "idle", "dnd", "invisible"]] = None, *, testo: Optional[str] = None):
        """Mostra lo stato del bot (staff autorizzato: può modificarlo)."""
        if stato or testo:
            if not (await self.bot.is_owner(ctx.author) or has_perm(ctx.author, (), ("manage_bot_status",))):
                raise NoPerm(["Manage Bot Status"])
            cfg = store.data.get("presence") or {}
            if stato:
                cfg["status"] = stato
            if testo:
                cfg["text"] = testo[:100]
            store.data["presence"] = cfg
            store.touch()
            await apply_presence(self.bot, cfg)
            return await ctx.send("✅ Stato del bot aggiornato.")
        up = int(time.time() - START_TIME)
        e = emb("🟢 Bot operativo")
        e.add_field(name="Latenza", value=f"{round(self.bot.latency * 1000)} ms")
        e.add_field(name="Online da", value=f"{up // 86400}g {up % 86400 // 3600}h {up % 3600 // 60}m")
        e.add_field(name="Server", value=len(self.bot.guilds))
        await ctx.send(embed=e)


# ============================================================================
#  13. REACTION ROLE (pulsanti)
# ============================================================================
def rr_view(role_ids, guild):
    v = discord.ui.View(timeout=None)
    for rid in role_ids[:25]:
        r = guild.get_role(rid)
        if r:
            v.add_item(discord.ui.Button(label=r.name[:80], style=discord.ButtonStyle.secondary, custom_id=f"rr:{rid}"))
    return v


def rr_embed(title, role_ids, mode):
    txt = "Premi un pulsante per ottenere (o togliere) il ruolo:\n" + "\n".join(f"• <@&{r}>" for r in role_ids)
    if mode == "unique":
        txt += "\n\n_Puoi avere un solo ruolo di questo pannello alla volta._"
    return emb(title, txt)


class RolePickView(discord.ui.View):
    def __init__(self, owner_id, callback):
        super().__init__(timeout=300)
        self.owner_id, self.cb = owner_id, callback
        self.sel = discord.ui.RoleSelect(placeholder="Scegli i ruoli del pannello", min_values=1, max_values=25)
        self.sel.callback = self.picked
        self.add_item(self.sel)

    async def interaction_check(self, i):
        if i.user.id != self.owner_id:
            await i.response.send_message("Non è il tuo menu.", ephemeral=True)
            return False
        return True

    async def picked(self, i):
        bad = [r for r in self.sel.values if r.is_default() or r.managed or r >= i.guild.me.top_role
               or (i.user.id != i.guild.owner_id and r >= i.user.top_role)]
        if bad:
            return await i.response.send_message("❌ Non posso/puoi gestire: " + ", ".join(r.name for r in bad), ephemeral=True)
        await self.cb(i, [r.id for r in self.sel.values])


class ReactionRoles(commands.Cog, name="Reaction Role"):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="reactionrole", invoke_without_command=True)
    async def reactionrole(self, ctx):
        """Ruoli tramite pulsanti."""
        await ctx.send("Usa `reactionrole create/edit/delete/list/setup`.")

    @reactionrole.command(name="create")
    @require(("manage_roles",))
    @commands.bot_has_permissions(manage_roles=True)
    async def rr_create(self, ctx, canale: discord.TextChannel, *, titolo: str = "Scegli i tuoi ruoli"):
        """Crea un messaggio con pulsanti per ottenere ruoli."""
        d = gd(ctx.guild)

        async def done(i, ids):
            mode = d["rr_cfg"]["mode"]
            msg = await canale.send(embed=rr_embed(titolo, ids, mode), view=rr_view(ids, i.guild))
            d["reactionroles"][str(msg.id)] = {"channel": canale.id, "title": titolo, "roles": ids, "mode": mode}
            store.touch()
            await i.response.edit_message(content=f"✅ Reaction role creato in {canale.mention}.", view=None)
        await ctx.send("Seleziona i ruoli da includere:", view=RolePickView(ctx.author.id, done), ephemeral=True)

    @reactionrole.command(name="edit")
    @require(("manage_roles",))
    async def rr_edit(self, ctx, message_id: str):
        """Modifica i ruoli di un reaction role esistente (ID messaggio)."""
        d = gd(ctx.guild)
        rec = d["reactionroles"].get(message_id)
        if not rec:
            return await fail(ctx, "Reaction role non trovato (controlla con `reactionrole list`).")

        async def done(i, ids):
            rec["roles"] = ids
            store.touch()
            try:
                msg = await i.guild.get_channel(rec["channel"]).fetch_message(int(message_id))
                await msg.edit(embed=rr_embed(rec["title"], ids, rec["mode"]), view=rr_view(ids, i.guild))
            except (discord.HTTPException, AttributeError):
                pass
            await i.response.edit_message(content="✅ Reaction role aggiornato.", view=None)
        await ctx.send("Seleziona i nuovi ruoli:", view=RolePickView(ctx.author.id, done), ephemeral=True)

    @reactionrole.command(name="delete")
    @require(("manage_roles",))
    async def rr_delete(self, ctx, message_id: str):
        """Elimina un sistema Reaction Role (ID messaggio)."""
        rec = gd(ctx.guild)["reactionroles"].pop(message_id, None)
        if not rec:
            return await fail(ctx, "Reaction role non trovato.")
        store.touch()
        try:
            await (await ctx.guild.get_channel(rec["channel"]).fetch_message(int(message_id))).delete()
        except (discord.HTTPException, AttributeError):
            pass
        await ctx.send("🗑️ Reaction role eliminato.")

    @reactionrole.command(name="list")
    @require(("manage_roles",))
    async def rr_list(self, ctx):
        """Mostra i sistemi Reaction Role configurati."""
        lines = [f"`{mid}` · <#{r['channel']}> · {len(r['roles'])} ruoli · {r['mode']}" for mid, r in gd(ctx.guild)["reactionroles"].items()]
        await ctx.send(embed=emb("🎭 Reaction role", "\n".join(lines) or "Nessuno."), ephemeral=True)

    @reactionrole.command(name="setup")
    @require(("manage_roles",))
    async def rr_setup(self, ctx, modalita: Literal["toggle", "unique"]):
        """Modalità dei nuovi pannelli: toggle (più ruoli) o unique (uno solo)."""
        gd(ctx.guild)["rr_cfg"]["mode"] = modalita
        store.touch()
        await ctx.send(f"✅ Modalità predefinita dei nuovi pannelli: **{modalita}**.")


async def handle_reaction_role(i, role_id):
    rec = gd(i.guild)["reactionroles"].get(str(i.message.id))
    role = i.guild.get_role(role_id)
    if not rec or not role or role_id not in rec["roles"]:
        return await i.response.send_message("❌ Questo pannello non è più valido.", ephemeral=True)
    if role >= i.guild.me.top_role:
        return await i.response.send_message("❌ Il mio ruolo è troppo basso per assegnare questo ruolo.", ephemeral=True)
    m = i.user
    if role in m.roles:
        await m.remove_roles(role, reason="Reaction role")
        return await i.response.send_message(f"➖ Ruolo **{role.name}** rimosso.", ephemeral=True)
    if rec["mode"] == "unique":
        others = [r for r in (i.guild.get_role(x) for x in rec["roles"]) if r and r in m.roles]
        if others:
            await m.remove_roles(*others, reason="Reaction role unique")
    await m.add_roles(role, reason="Reaction role")
    await i.response.send_message(f"➕ Ruolo **{role.name}** assegnato.", ephemeral=True)


# ============================================================================
#  14. GIVEAWAY
# ============================================================================
def gw_embed(rec, ended=False):
    e = emb(f"🎉 {rec['prize']}", color=0x99AAB5 if ended else 0xEB459E)
    e.add_field(name="Vincitori", value=rec["winners"])
    e.add_field(name="Organizzato da", value=f"<@{rec['host']}>")
    if ended:
        w = " ".join(f"<@{x}>" for x in rec.get("winner_ids", [])) or "Nessun partecipante valido"
        e.add_field(name="🏆 Esito", value=w, inline=False)
        e.set_footer(text=f"Terminato · {len(rec['participants'])} partecipanti")
    else:
        e.add_field(name="Termina", value=tstamp(rec["end"]), inline=False)
        e.set_footer(text="Premi il pulsante per partecipare (premi di nuovo per uscire)")
    return e


async def end_giveaway(bot, guild, mid, rec):
    rec["ended"] = True
    cands = [u for u in rec["participants"] if guild.get_member(u)]
    rec["winner_ids"] = random.sample(cands, min(len(cands), rec["winners"]))
    store.touch()
    ch = guild.get_channel(rec["channel"])
    if not ch:
        return
    try:
        msg = await ch.fetch_message(int(mid))
        await msg.edit(embed=gw_embed(rec, True), view=None)
    except discord.HTTPException:
        pass
    mentions = " ".join(f"<@{x}>" for x in rec["winner_ids"])
    try:
        await ch.send(f"🎊 Complimenti {mentions}! Avete vinto **{rec['prize']}**!" if mentions
                      else f"😕 Nessun partecipante valido per **{rec['prize']}**.",
                      allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException:
        pass


async def handle_giveaway_join(i):
    rec = gd(i.guild)["giveaways"].get(str(i.message.id))
    if not rec or rec["ended"]:
        return await i.response.send_message("❌ Questo giveaway è terminato.", ephemeral=True)
    if i.user.id in rec["participants"]:
        rec["participants"].remove(i.user.id)
        txt = "👋 Hai lasciato il giveaway."
    else:
        rec["participants"].append(i.user.id)
        txt = "✅ Sei dentro! In bocca al lupo 🍀"
    store.touch()
    await i.response.send_message(txt, ephemeral=True)


class Giveaways(commands.Cog, name="Giveaway"):
    def __init__(self, bot):
        self.bot = bot
        self.task = None

    async def cog_load(self):
        self.task = asyncio.create_task(self.loop())

    async def cog_unload(self):
        if self.task:
            self.task.cancel()

    async def loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                now = time.time()
                for gid, g in list(store.data["guilds"].items()):
                    guild = self.bot.get_guild(int(gid))
                    if not guild:
                        continue
                    for mid, rec in list(g.get("giveaways", {}).items()):
                        if not rec["ended"] and rec["end"] <= now:
                            await end_giveaway(self.bot, guild, mid, rec)
            except Exception:
                traceback.print_exc()
            await asyncio.sleep(15)

    @commands.hybrid_group(name="giveaway", invoke_without_command=True)
    async def giveaway(self, ctx):
        """Gestione giveaway."""
        await ctx.send("Usa `giveaway create/end/list/delete`.")

    @giveaway.command(name="create")
    @require((), ("manage_giveaways",))
    async def gw_create(self, ctx, durata: str, vincitori: commands.Range[int, 1, 20], *, premio: str):
        """Crea un giveaway (durata es. 30m, 2h, 1d) nel canale corrente."""
        secs = parse_duration(durata)
        if not secs or secs < 10 or secs > 60 * 86400:
            return await fail(ctx, "Durata non valida (da 10s a 60d, es. `1h30m`).")
        rec = {"channel": ctx.channel.id, "prize": premio[:200], "end": int(time.time()) + secs, "winners": vincitori,
               "host": ctx.author.id, "participants": [], "ended": False}
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="Partecipa", emoji="🎉", style=discord.ButtonStyle.success, custom_id="gw:join"))
        msg = await ctx.channel.send(embed=gw_embed(rec), view=v)
        gd(ctx.guild)["giveaways"][str(msg.id)] = rec
        store.touch()
        await ctx.send("✅ Giveaway creato.", ephemeral=True)

    @giveaway.command(name="end")
    @require((), ("manage_giveaways",))
    async def gw_end(self, ctx, message_id: str):
        """Termina subito un giveaway (ID messaggio) ed estrae i vincitori."""
        rec = gd(ctx.guild)["giveaways"].get(message_id)
        if not rec or rec["ended"]:
            return await fail(ctx, "Giveaway non trovato o già terminato.")
        await end_giveaway(self.bot, ctx.guild, message_id, rec)
        await ctx.send("✅ Giveaway terminato.", ephemeral=True)

    @giveaway.command(name="list")
    async def gw_list(self, ctx):
        """Mostra i giveaway attivi (o tutti, per lo staff)."""
        full = has_perm(ctx.author, (), ("manage_giveaways",))
        lines = []
        for mid, r in gd(ctx.guild)["giveaways"].items():
            if r["ended"] and not full:
                continue
            lines.append(f"`{mid}` · **{r['prize']}** · " + ("terminato" if r["ended"] else f"termina {tstamp(r['end'])}")
                         + f" · {len(r['participants'])} partecipanti")
        await ctx.send(embed=emb("🎉 Giveaway", cut("\n".join(lines) or "Nessun giveaway.", 4000)))

    @giveaway.command(name="delete")
    @require((), ("manage_giveaways",))
    async def gw_delete(self, ctx, message_id: str):
        """Elimina un giveaway dal sistema (ID messaggio)."""
        rec = gd(ctx.guild)["giveaways"].pop(message_id, None)
        if not rec:
            return await fail(ctx, "Giveaway non trovato.")
        store.touch()
        try:
            await (await ctx.guild.get_channel(rec["channel"]).fetch_message(int(message_id))).delete()
        except (discord.HTTPException, AttributeError):
            pass
        await ctx.send("🗑️ Giveaway eliminato.")


# ============================================================================
#  15-16. STATISTICHE E AFK
# ============================================================================
class Stats(commands.Cog, name="Statistiche"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        d = gd(message.guild)
        d["stats"]["messages"] += 1
        d["activity"][str(message.author.id)] = d["activity"].get(str(message.author.id), 0) + 1
        store.touch()

    @commands.hybrid_group(name="leaderboard", invoke_without_command=True)
    async def leaderboard(self, ctx):
        """Classifiche."""
        await ctx.send("Usa `leaderboard activity`.")

    @leaderboard.command(name="activity")
    async def lb_activity(self, ctx):
        """Mostra la classifica dei membri più attivi."""
        items = sorted(gd(ctx.guild)["activity"].items(), key=lambda x: -x[1])
        await ctx.send(embed=emb("🔥 Membri più attivi", top_lines(ctx.guild, items, lambda v: f"{v} messaggi")))


class AFK(commands.Cog, name="AFK"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        d = gd(message.guild)
        afk = d["afk"]
        low = message.content.lower()
        if str(message.author.id) in afk and not (low.startswith("star afk") or low.startswith("/afk")):
            afk.pop(str(message.author.id))
            store.touch()
            try:
                await message.channel.send(f"👋 Bentornato {message.author.mention}, ho rimosso il tuo AFK.", delete_after=8)
            except discord.HTTPException:
                pass
        for m in message.mentions:
            rec = afk.get(str(m.id))
            if rec and m.id != message.author.id:
                try:
                    await message.channel.send(f"💤 **{m.display_name}** è AFK ({tstamp(rec['ts'])}): {rec['msg']}",
                                               allowed_mentions=discord.AllowedMentions.none(), delete_after=15)
                except discord.HTTPException:
                    pass

    @commands.hybrid_group(name="afk", invoke_without_command=True)
    async def afk(self, ctx):
        """Stato AFK."""
        await ctx.send("Usa `afk set [messaggio]`, `afk remove`, `afk info [membro]`.")

    @afk.command(name="set")
    async def afk_set(self, ctx, *, messaggio: str = "AFK"):
        """Imposta il tuo stato AFK con un messaggio."""
        gd(ctx.guild)["afk"][str(ctx.author.id)] = {"msg": cut(messaggio, 150), "ts": int(time.time())}
        store.touch()
        await ctx.send(f"💤 {ctx.author.mention} ora è AFK: {cut(messaggio, 150)}", allowed_mentions=discord.AllowedMentions.none())

    @afk.command(name="remove")
    async def afk_remove(self, ctx):
        """Rimuove il tuo stato AFK."""
        if gd(ctx.guild)["afk"].pop(str(ctx.author.id), None) is None:
            return await fail(ctx, "Non sei AFK.")
        store.touch()
        await ctx.send("✅ Stato AFK rimosso.")

    @afk.command(name="info")
    async def afk_info(self, ctx, member: Optional[discord.Member] = None):
        """Mostra lo stato AFK di un membro."""
        m = member or ctx.author
        rec = gd(ctx.guild)["afk"].get(str(m.id))
        await ctx.send(f"💤 **{m.display_name}** è AFK da {tstamp(rec['ts'])}: {rec['msg']}" if rec else f"{m.display_name} non è AFK.",
                       allowed_mentions=discord.AllowedMentions.none())


# ============================================================================
#  17. AUTOMOD
# ============================================================================
class AutoMod(commands.Cog, name="AutoMod"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return
        am = gd(message.guild)["automod"]
        if not am["enabled"] or not am["words"] or has_perm(message.author, ("manage_messages",)):
            return
        text = message.content.lower()
        hit = next((w for w in am["words"] if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text)), None)
        if not hit:
            return
        try:
            await message.delete()
            await message.channel.send(f"🚫 {message.author.mention} quel linguaggio non è consentito.", delete_after=6)
        except discord.HTTPException:
            pass
        am["logs"].append({"user": message.author.id, "word": hit, "channel": message.channel.id, "ts": int(time.time())})
        del am["logs"][:-100]
        store.touch()

    @commands.hybrid_group(name="automod", invoke_without_command=True)
    async def automod(self, ctx):
        """Protezioni automatiche."""
        await ctx.send("Usa `automod enable/disable/rules/logs` e `automod word add/remove`.")

    @automod.command(name="enable")
    @require(("manage_guild",), ("manage_automod",))
    async def am_enable(self, ctx):
        """Attiva le regole AutoMod del bot."""
        gd(ctx.guild)["automod"]["enabled"] = True
        store.touch()
        await ctx.send("✅ AutoMod attivato.")

    @automod.command(name="disable")
    @require(("manage_guild",), ("manage_automod",))
    async def am_disable(self, ctx):
        """Disattiva le regole AutoMod del bot."""
        gd(ctx.guild)["automod"]["enabled"] = False
        store.touch()
        await ctx.send("✅ AutoMod disattivato.")

    @automod.command(name="rules")
    async def am_rules(self, ctx):
        """Mostra le regole configurate."""
        d = gd(ctx.guild)
        am = d["automod"]
        onoff = lambda b: "🟢" if b else "🔴"
        txt = (f"{onoff(am['enabled'])} Filtro parole vietate ({len(am['words'])})\n"
               f"{onoff(d['antispam']['enabled'])} Antispam\n{onoff(d['antilink']['enabled'])} Antilink")
        if has_perm(ctx.author, ("manage_guild",), ("manage_automod",)):
            txt += "\n\n**Parole vietate:** " + (", ".join(f"||{w}||" for w in am["words"]) or "nessuna")
        await ctx.send(embed=emb("🤖 Regole AutoMod", txt), ephemeral=True)

    @automod.command(name="logs")
    @require(("manage_guild",), ("view_automod_logs",))
    async def am_logs(self, ctx):
        """Mostra gli eventi registrati dalle protezioni automatiche."""
        logs = gd(ctx.guild)["automod"]["logs"][-15:]
        lines = [f"{tstamp(x['ts'], 'R')} · <@{x['user']}> · ||{x['word']}|| · <#{x['channel']}>" for x in logs]
        await ctx.send(embed=emb("📜 Log AutoMod", "\n".join(lines) or "Nessun evento."), ephemeral=True)

    @automod.group(name="word", invoke_without_command=True)
    async def am_word(self, ctx):
        """Parole vietate."""
        await ctx.send("Usa `automod word add <parola>` o `automod word remove <parola>`.")

    @am_word.command(name="add")
    @require(("manage_guild",), ("manage_automod",))
    async def am_word_add(self, ctx, *, parola: str):
        """Aggiunge una parola vietata."""
        w = parola.strip().lower()
        words = gd(ctx.guild)["automod"]["words"]
        if w and w not in words:
            words.append(w)
            store.touch()
        await ctx.send("✅ Parola aggiunta.", ephemeral=True)

    @am_word.command(name="remove")
    @require(("manage_guild",), ("manage_automod",))
    async def am_word_remove(self, ctx, *, parola: str):
        """Rimuove una parola vietata."""
        words = gd(ctx.guild)["automod"]["words"]
        if parola.strip().lower() in words:
            words.remove(parola.strip().lower())
            store.touch()
        await ctx.send("✅ Parola rimossa.", ephemeral=True)


# ============================================================================
#  ROUTER INTERAZIONI (pulsanti/menu persistenti: restano attivi dopo il riavvio)
# ============================================================================
async def handle_verify(i):
    v = gd(i.guild)["verification"]
    role = i.guild.get_role(v["role"]) if v["role"] else None
    if not v["enabled"] or not role:
        return await i.response.send_message("❌ La verifica è disattivata.", ephemeral=True)
    if role in i.user.roles:
        return await i.response.send_message("✅ Sei già verificato.", ephemeral=True)
    await i.user.add_roles(role, reason="Verifica")
    await i.response.send_message("✅ Verifica completata, benvenuto!", ephemeral=True)


async def route_interaction(interaction):
    if interaction.type is not discord.InteractionType.component or not interaction.guild:
        return
    data = interaction.data or {}
    cid = data.get("custom_id", "")
    try:
        if cid.startswith("tkt:open:"):
            _, _, pid, cat = cid.split(":")
            await open_ticket(interaction, pid, cat)
        elif cid.startswith("tkt:menu:"):
            await open_ticket(interaction, cid.split(":")[2], data["values"][0])
        elif cid.startswith("tktb:"):
            await handle_ticket_button(interaction, cid[5:])
        elif cid == "verify:btn":
            await handle_verify(interaction)
        elif cid.startswith("rr:"):
            await handle_reaction_role(interaction, int(cid[3:]))
        elif cid == "gw:join":
            await handle_giveaway_join(interaction)
    except Exception:
        traceback.print_exc()
        try:
            msg = "❌ Si è verificato un errore (controlla i miei permessi)."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


# ============================================================================
#  BOT
# ============================================================================
async def get_prefix(bot, message):
    c = message.content
    prefixes = [f"<@{bot.user.id}> ", f"<@!{bot.user.id}> "]
    if c[:5].lower() == "star ":
        prefixes.insert(0, c[:5])  # accetta star / Star / STAR (e qualsiasi maiuscola)
    return prefixes


class StarBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=get_prefix, intents=discord.Intents.all(), help_command=None,
                         case_insensitive=True,
                         allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
        self.add_check(self._guild_only)

    @staticmethod
    async def _guild_only(ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def setup_hook(self):
        for cog in (Moderation, Welcome, Security, Tickets, Partnership, Economy, Levels, Games, Utility, Config,
                    Logs, Custom, StatusCog, ReactionRoles, Giveaways, Stats, AFK, AutoMod):
            await self.add_cog(cog(self))
        self.add_listener(route_interaction, "on_interaction")
        self.autosave_task = asyncio.create_task(self._autosave())
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

    async def _autosave(self):
        while True:
            await asyncio.sleep(20)
            try:
                store.flush()
            except Exception:
                traceback.print_exc()

    async def on_ready(self):
        await apply_presence(self, store.data.get("presence"))
        print(f"✅ Online come {self.user} ({self.user.id}) in {len(self.guilds)} server")

    async def on_command_error(self, ctx, error):
        await handle_error(ctx, error)

    async def close(self):
        store.flush()
        await super().close()


bot = StarBot()


@bot.tree.error
async def on_app_command_error(interaction, error):
    await handle_error(interaction, error)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Imposta la variabile d'ambiente DISCORD_TOKEN con il token del bot.")
    bot.run(TOKEN)
