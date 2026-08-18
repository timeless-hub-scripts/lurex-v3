import asyncio
import io
import json
import os
import sys
import time

import discord

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from darcobfuscator import __version__
    from darcobfuscator.obfuscator import obfuscate as _local_obfuscate
except Exception:
    # The GitHub upload may flatten the package files at repository root.
    # Load that root as the darcobfuscator package so relative imports still work.
    try:
        import importlib.util
        _base_dir = os.path.dirname(os.path.abspath(__file__))
        _spec = importlib.util.spec_from_file_location(
            "darcobfuscator",
            os.path.join(_base_dir, "__init__.py"),
            submodule_search_locations=[_base_dir],
        )
        _pkg = importlib.util.module_from_spec(_spec)
        sys.modules["darcobfuscator"] = _pkg
        _spec.loader.exec_module(_pkg)
        from darcobfuscator import __version__
        from darcobfuscator.obfuscator import obfuscate as _local_obfuscate
    except Exception:
        _local_obfuscate = None
        __version__ = os.environ.get("LUREX_VERSION", os.environ.get("FINE_VERSION", "11.2"))

TOKEN = os.environ.get("TOKEN") or os.environ.get("DISCORD_TOKEN")
MAX_BYTES = int(os.environ.get("MAX_BYTES", "1000000"))
API_URL = os.environ.get("API_URL", "https://<your-project>.vercel.app/api")
OBF_BACKEND = os.environ.get("OBF_BACKEND", "local").lower()
BOT_SHARED_SECRET = os.environ.get("BOT_SHARED_SECRET")
OWNER_VIEW_IDS = {x.strip() for x in os.environ.get("OWNER_VIEW_IDS", "").split(",") if x.strip()}
GUILD_IDS = [int(g) for g in os.environ.get("GUILD_IDS", "").replace(" ", "").split(",") if g.isdigit()] or None
ALLOWED_EXT = (".lua", ".luau", ".txt")

# Website-created scripts are linked to a Discord server explicitly with /login.
# Sessions are scoped by guild and script, so logging in one server never grants
# access to another server or another script.
ACTIVE_SCRIPT_SESSIONS = {}

BRAND = "LUREX"
COL_PROC = 0x5865F2
COL_OK = 0x57F287
COL_ERR = 0xED4245
COL_IDLE = 0x2B2D31
COL_KEY = 0xFEE75C
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
STAGE_ICON = "◆"


def _use_api():
    return OBF_BACKEND == "api"


async def _api_call(payload):
    import aiohttp
    headers = {}
    if BOT_SHARED_SECRET:
        headers["x-lurex-auth"] = BOT_SHARED_SECRET
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        async with session.post(API_URL, json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(str(data.get("error", f"API returned {resp.status}")))
            return data


async def _service_build(source, opts, owner=None):
    if _use_api():
        return await _api_call({"source": source, "options": opts, "owner": owner})
    if _local_obfuscate is None:
        raise RuntimeError("Local obfuscator unavailable — set OBF_BACKEND=api and API_URL.")
    loop = asyncio.get_event_loop()
    output = await loop.run_in_executor(None, lambda: _local_obfuscate(source, dict(opts)))
    return {"output": output, "loadstring": None, "url": None, "script_id": None,
            "name": opts.get("name"), "ephemeral_storage": True, "bytes": len(output)}


async def _api_manage(payload):
    if not _use_api():
        raise RuntimeError("Hosted management needs the API backend (set OBF_BACKEND=api and API_URL).")
    return await _api_call(payload)


def _session_key(ctx, script_id):
    return (str(getattr(ctx, "guild_id", "") or ""), str(script_id or "").strip())


def _is_script_logged_in(ctx, script_id):
    return bool(getattr(ctx, "guild_id", None) and ACTIVE_SCRIPT_SESSIONS.get(_session_key(ctx, script_id)))


async def _require_script_login(ctx, script_id, role_field=None):
    if not getattr(ctx, "guild_id", None):
        await ctx.respond(embed=_err_embed("This command must be used inside a Discord server."), ephemeral=True)
        return False
    if not _is_script_logged_in(ctx, script_id):
        await ctx.respond(embed=_err_embed(f"This server is not logged in to `{script_id}`. Run `/login script_id:{script_id}` first."), ephemeral=True)
        return False
    if role_field and getattr(ctx, "author", None):
        try:
            info = await _api_manage({"action": "info", "script_id": script_id})
            configured_role = str(info.get(role_field) or "").strip()
            if configured_role:
                member_role_ids = {str(role.id) for role in getattr(ctx.author, "roles", [])}
                is_admin = bool(getattr(getattr(ctx.author, "guild_permissions", None), "administrator", False))
                if configured_role not in member_role_ids and not is_admin:
                    await ctx.respond(embed=_err_embed(f"You need the configured Discord role for `{script_id}` to use this command."), ephemeral=True)
                    return False
        except Exception as exc:
            await ctx.respond(embed=_err_embed(f"Could not verify the configured script role: {str(exc)[:250]}"), ephemeral=True)
            return False
    return True


intents = discord.Intents.default()
intents.message_content = True
bot = discord.Bot(intents=intents)


def _interaction(a, b):
    return a if hasattr(a, "response") else b


def _other(a, b):
    return b if hasattr(a, "response") else a


def _bar(pct, width=20):
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "▰"* filled + "▱"* (width - filled)


def _human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}"if unit == "B"else f"{n:.1f} {unit}"
        n /= 1024


def _proc_embed(spin, stage, pct):
    e = discord.Embed(title=f"{spin}  Obfuscating", color=COL_PROC)
    e.description = f"{STAGE_ICON} **{stage}**\n\n```{_bar(pct)}```\n`{pct:5.1f}%`"
    e.set_footer(text=f"{BRAND} v{__version__}")
    return e


def _ok_embed(name, script_name, in_bytes, out_bytes, secs, silent, full_protect):
    ratio = (out_bytes / in_bytes) if in_bytes else 0
    flags = []
    flags.append("silent"if silent else "prints")
    if full_protect:
        flags.append("FULL PROTECT")
    e = discord.Embed(title="Obfuscation complete", color=COL_OK,
                      description=f"**{script_name}** is protected and hosted.")
    e.add_field(name="Source", value=f"`{name}`\n{_human(in_bytes)}", inline=True)
    e.add_field(name="Protected", value=f"{_human(out_bytes)}\n`{ratio:.2f}x`", inline=True)
    e.add_field(name="Build", value=f"Executor · {secs:.2f}s\n{'· '.join(flags)}", inline=True)
    e.set_footer(text=f"{BRAND} v{__version__}  •  single-line • per-build VM")
    return e


def _copy_block(label, value):
    value = str(value or "")
    mobile = value.replace("\n", ";")
    return (f"**{label} — PC COPY**\n```\n{value}\n```\n"
            f"**{label} — MOBILE COPY**\n`{mobile}`")

def _loader_lines(desktop):
    return _copy_block("LOADSTRING", desktop)
def _loader_embed(script_name, loadstring, ephemeral_storage):
    e = discord.Embed(title="Your loader", color=COL_OK,
                      description=(f"Run **{script_name}** on your executor:\n"
                                   f"{_loader_lines(loadstring)}\n"
                                   "This URL always serves the **latest** version you upload."))
    if ephemeral_storage:
        e.add_field(name="Storage not configured",
                    value=("The API has no database attached, so this loader is **temporary** and "
                           "will vanish on the next redeploy. Attach Neon Postgres "
                           "(`DATABASE_URL`) to persist it."),
                    inline=False)
    e.set_footer(text=f"{BRAND} v{__version__}")
    return e


def _key_embed(owner_key, project_id=None, free=False, script_id=None):
    desc = ["**Owner credential** (shown once — save it):", _copy_block("OWNER KEY", owner_key),
            "Use the owner credential for information, management, and permission commands."]
    if script_id:
        desc += ["", "**Script ID** (for script update and panel actions):", _copy_block("SCRIPT ID", script_id)]
    if project_id:
        desc += ["", f"**Project ID** (public — for `/deploy`):", f"`{project_id}`"]
        if not free:
            desc += ["Key required: run **`/gkey`** to mint keys, then post a "
                     "**`/deploy`** so buyers can redeem them."]
        else:
            desc += ["Free: buyers can run it without a key."]
    e = discord.Embed(title="Owner & project keys", color=COL_KEY, description="\n".join(desc))
    e.set_footer(text=f"{BRAND} v{__version__}  •  keep the owner key private")
    return e


def _err_embed(msg):
    e = discord.Embed(title="Could not obfuscate", color=COL_ERR, description=msg)
    e.set_footer(text=f"{BRAND} v{__version__}")
    return e


def _config_embed(name, silent, full_protect, free):
    lines = [
        f"**Source:** `{name}`",
        "",
        f"{'**Silent Mode** — on (no prints)'if silent else '**Silent Mode** — off (prints a load banner)'}",
        f"{'**FULL PROTECT** — on'if full_protect else '**FULL PROTECT** — off (lighter protection)'}",
        f"{'**Access** — free (no key required)'if free else '**Access** — key required (HWID-locked)'}",
    ]
    if full_protect:
        lines += ["", "**FULL PROTECT is enabled** — all available protection checks are applied."]
    if not free:
        lines += ["", "With a key required, generate keys with **`/gkey`** and give buyers a "
                  "**`/deploy`** to redeem them. Each key is HWID-locked on first run."]
    lines += ["", "Toggle the options, then press **Name & protect**."]
    e = discord.Embed(title="Configure your build", color=COL_PROC, description="\n".join(lines))
    e.set_footer(text=f"{BRAND} v{__version__}  •  executor build")
    return e


def _help_embed():
    e = discord.Embed(
        title=f"{BRAND} obfuscator",
        color=COL_IDLE,
        description=(
            "**DM me a `.lua` / `.luau` file** and I'll protect it, host it, and hand you a"
            "ready-to-run **loadstring**.\n\n"
            "• Pick a **name**, then toggle **Silent** / **FULL PROTECT** mode.\n"
            "• You get a private **script key** to update, freeze, or delete your script"
            "with `/manage` — the loadstring never changes.\n"

        ),
    )
    e.set_footer(text=f"{BRAND} v{__version__}")
    return e


class Session:
    def __init__(self, author_id, origin, filename, data, channel=None):
        self.author_id = author_id
        self.origin = origin
        self.filename = filename
        self.data = data
        self.channel = channel
        self.server_id = None
        self.default_name = os.path.splitext(filename)[0][:64] or "script"
        self.name = self.default_name
        self.silent = False
        self.full_protect = True
        self.engine = "LUREX"
        self.preset = "Strong"
        self.free = False


class ChannelResponder:
    def __init__(self, channel, status):
        self.channel = channel
        self.status = status

    async def animate(self, embed):
        if self.status is None:
            return
        try:
            await self.status.edit(embed=embed)
        except discord.HTTPException:
            pass

    async def result(self, ok_embed, file, loader_embed, view, key_embed):
        await self.channel.send(embed=ok_embed, file=file)
        await self.channel.send(embed=loader_embed, view=view)
        if key_embed is not None:
            await self.channel.send(embed=key_embed)
        if self.status is not None:
            try:
                await self.status.delete()
            except discord.HTTPException:
                pass

    async def err(self, embed):
        if self.status is not None:
            try:
                await self.status.edit(embed=embed)
                return
            except discord.HTTPException:
                pass
        await self.channel.send(embed=embed)


class EphemeralResponder:
    def __init__(self, interaction):
        self.interaction = interaction

    async def animate(self, embed):
        try:
            await self.interaction.edit_original_response(embed=embed)
        except discord.HTTPException:
            pass

    async def result(self, ok_embed, file, loader_embed, view, key_embed):
        try:
            await self.interaction.edit_original_response(embed=ok_embed)
        except discord.HTTPException:
            pass
        await self.interaction.followup.send(embed=loader_embed, file=file, view=view, ephemeral=True)
        if key_embed is not None:
            await self.interaction.followup.send(embed=key_embed, ephemeral=True)

    async def err(self, embed):
        try:
            await self.interaction.edit_original_response(embed=embed)
        except discord.HTTPException:
            await self.interaction.followup.send(embed=embed, ephemeral=True)


async def _animate(responder, state):
    i = 0
    while not state["done"]:
        if state["pct"] < state["target"]:
            state["pct"] = min(state["target"], state["pct"] + max(1.5, (state["target"] - state["pct"]) / 3))
        await responder.animate(_proc_embed(SPIN[i % len(SPIN)], state["stage"], state["pct"]))
        i += 1
        await asyncio.sleep(0.85)


async def _do(responder, session):
    opts = {"target": "executor", "name": session.name,
            "silent": session.silent, "fast": not session.full_protect, "free": session.free,
            "engine": session.engine, "preset": session.preset}
    state = {"stage": "Reading source", "pct": 2.0, "target": 22.0, "done": False}
    anim = asyncio.create_task(_animate(responder, state))
    start = time.time()
    try:
        source = session.data.decode("utf-8", "replace")
        await asyncio.sleep(0.4)
        state["stage"], state["target"] = "Compiling & virtualizing", 60.0
        owner_meta = json.dumps({"user_id": str(session.author_id), "server_id": str(session.server_id or "")})
        result = await _service_build(source, opts, owner=owner_meta)
        state["stage"], state["target"] = "Hosting & packaging", 95.0
        await asyncio.sleep(0.3)
    except SyntaxError as e:
        state["done"] = True
        anim.cancel()
        await responder.err(_err_embed(f"Syntax error in your script:\n```\n{str(e)[:400]}\n```"))
        return
    except Exception as e:
        state["done"] = True
        anim.cancel()
        await responder.err(_err_embed(f"```\n{str(e)[:400]}\n```"))
        return
    state["pct"], state["target"], state["done"] = 100.0, 100.0, True
    anim.cancel()
    elapsed = time.time() - start

    output = result["output"]
    payload = output.encode("utf-8")
    out_name = os.path.splitext(session.filename)[0] + ".obfuscated.lua"
    file = discord.File(io.BytesIO(payload), filename=out_name)

    ok_embed = _ok_embed(session.filename, session.name, len(session.data),
                         len(payload), elapsed, session.silent, session.full_protect)

    loadstring = result.get("loadstring")
    script_id = result.get("script_id")
    if loadstring:
        loader_embed = _loader_embed(session.name, loadstring, result.get("ephemeral_storage", False))
        view = ResultView(session.author_id, script_id, session.name) if script_id else None
        key_embed = _key_embed(result.get("owner_key") or script_id, result.get("project_id"), session.free, script_id) if script_id and not session.free else None
    else:
        loader_embed = discord.Embed(
            title="Protected file ready", color=COL_OK,
            description=("Hosting is off, so there's no loadstring or script key.\n"
                         "Set **`OBF_BACKEND=api`** and **`API_URL`** to get a hosted loader."))
        loader_embed.set_footer(text=f"{BRAND} v{__version__}")
        view = None
        key_embed = None

    await responder.result(ok_embed, file, loader_embed, view, key_embed)


async def _launch_build(session, interaction):
    if session.origin == "slash":
        await interaction.response.defer(ephemeral=True)
        await _do(EphemeralResponder(interaction), session)
    else:
        await interaction.response.defer()
        status = await session.channel.send(embed=_proc_embed(SPIN[0], "Queued", 2.0))
        await _do(ChannelResponder(session.channel, status), session)


class NameModal(discord.ui.Modal):
    def __init__(self, session):
        super().__init__(title="Add script name")
        self.session = session
        self.name_input = discord.ui.InputText(
            label="Script name",
            placeholder="e.g. Inferno Duels",
            value=session.default_name,
            max_length=64,
            required=True,
        )
        self.add_item(self.name_input)

    async def callback(self, interaction):
        value = (self.name_input.value or "").strip()
        self.session.name = value[:64] or "script"
        await interaction.response.edit_message(
            embed=_mode_embed(self.session), view=ModeView(self.session))


def _name_embed(session):
    e = discord.Embed(title="Name your script", color=COL_PROC,
                      description=(f"File: `{session.filename}`\n\n"
                                   "Press **ADD NAME** to choose the script name.\n"
                                   "The name is used in your script selector and panel."))
    e.set_footer(text=f"{BRAND} v{__version__}")
    return e


def _mode_embed(session):
    e = discord.Embed(title=f"{session.name} · choose access mode", color=COL_PROC,
                      description=("Choose how this script will be delivered.\n\n"
                                   "**FREE MODE 🆓** — anyone can use the loader.\n"
                                   "**PAID MODE 🔐** — buyers use LUREX keys through a panel."))
    e.set_footer(text=f"{BRAND} v{__version__}")
    return e


def _protection_embed(session):
    state = "PROMETHEUS" if session.engine == "PROMETHEUS" else ("FULL PROTECT" if session.full_protect else "SILENT MODE")
    e = discord.Embed(title=f"{session.name} · protection", color=COL_PROC,
                      description=(f"Access: **{'FREE MODE 🆓' if session.free else 'PAID MODE 🔐'}**\n"
                                   f"Selected: **{state}**\n\n"
                                   "Choose one protection mode, then press **NEXT**. Prometheus uses AST-based Lua/Luau obfuscation."))
    e.set_footer(text=f"{BRAND} v{__version__}")
    return e


class CreateNameView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=600)
        self.session = session

    async def interaction_check(self, interaction):
        if interaction.user.id != self.session.author_id:
            await interaction.response.send_message("This isn't your session.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="ADD NAME", style=discord.ButtonStyle.primary)
    async def add_name(self, a, b):
        interaction = _interaction(a, b)
        await interaction.response.send_modal(NameModal(self.session))


class ModeView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=600)
        self.session = session

    async def interaction_check(self, interaction):
        if interaction.user.id != self.session.author_id:
            await interaction.response.send_message("This isn't your session.", ephemeral=True)
            return False
        return True

    async def _choose(self, interaction, free):
        self.session.free = free
        await interaction.response.edit_message(embed=_protection_embed(self.session),
                                                 view=ProtectionView(self.session))
        self.stop()

    @discord.ui.button(label="FREE MODE 🆓", style=discord.ButtonStyle.success)
    async def free_mode(self, a, b):
        await self._choose(_interaction(a, b), True)

    @discord.ui.button(label="PAID MODE 🔐", style=discord.ButtonStyle.primary)
    async def paid_mode(self, a, b):
        await self._choose(_interaction(a, b), False)


class ProtectionView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=600)
        self.session = session

    async def interaction_check(self, interaction):
        if interaction.user.id != self.session.author_id:
            await interaction.response.send_message("This isn't your session.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction):
        await interaction.response.edit_message(embed=_protection_embed(self.session), view=self)

    @discord.ui.button(label="FULL PROTECT (LUREX)", style=discord.ButtonStyle.primary, row=0)
    async def full_protect(self, a, b):
        self.session.engine = "LUREX"
        self.session.full_protect = True
        self.session.silent = False
        await self._refresh(_interaction(a, b))

    @discord.ui.button(label="PROMETHEUS", style=discord.ButtonStyle.primary, row=0)
    async def prometheus(self, a, b):
        self.session.engine = "PROMETHEUS"
        self.session.full_protect = True
        self.session.silent = False
        await self._refresh(_interaction(a, b))

    @discord.ui.button(label="SILENT MODE", style=discord.ButtonStyle.secondary, row=1)
    async def silent_mode(self, a, b):
        self.session.engine = "LUREX"
        self.session.full_protect = False
        self.session.silent = True
        await self._refresh(_interaction(a, b))

    @discord.ui.button(label="NEXT", style=discord.ButtonStyle.success, row=1)
    async def next_step(self, a, b):
        interaction = _interaction(a, b)
        self.stop()
        await _launch_build(self.session, interaction)


class ResultView(discord.ui.View):
    def __init__(self, author_id, script_id, script_name, frozen=False):
        super().__init__(timeout=1800)
        self.author_id = author_id
        self.script_id = script_id
        self.script_name = script_name
        self.frozen = frozen

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your script.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Freeze", style=discord.ButtonStyle.secondary)
    async def freeze(self, a, b):
        interaction = _interaction(a, b)
        button = _other(a, b)
        action = "unfreeze"if self.frozen else "freeze"
        try:
            await _api_manage({"action": action, "script_id": self.script_id})
        except Exception as e:
            await interaction.response.send_message(embed=_err_embed(f"```\n{str(e)[:300]}\n```"), ephemeral=True)
            return
        self.frozen = not self.frozen
        button.label = "Unfreeze"if self.frozen else "Freeze"
        note = "frozen — the loadstring now returns a notice"if self.frozen else "live again"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"**{self.script_name}** is {note}.", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete(self, a, b):
        interaction = _interaction(a, b)
        try:
            await _api_manage({"action": "delete", "script_id": self.script_id})
        except Exception as e:
            await interaction.response.send_message(embed=_err_embed(f"```\n{str(e)[:300]}\n```"), ephemeral=True)
            return
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"**{self.script_name}** deleted — the loadstring is now dead.", ephemeral=True)


@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Streaming(
        name="Protecting Scripts For Free, 0 Execution Lag. DM me a Script File To Start!",
        url="https://discord.gg/jzVFxhETCn"))
    print(f"{BRAND} bot online as {bot.user} (v{__version__}) backend={OBF_BACKEND}")


async def _begin(author, origin, filename, data, channel=None, ctx=None):
    session = Session(author.id, origin, filename, data, channel)
    session.server_id = str(getattr(ctx, "guild_id", "") or "") if ctx is not None else ""
    embed = _name_embed(session)
    view = CreateNameView(session)
    if origin == "slash":
        await ctx.respond(embed=embed, view=view, ephemeral=True)
    else:
        await channel.send(embed=embed, view=view)


@bot.event
async def on_message(message):
    if message.author.bot or message.author == bot.user:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return
    files = [a for a in message.attachments if a.filename.lower().endswith(ALLOWED_EXT)]
    if not files:
        await message.channel.send(embed=_help_embed())
        return
    att = files[0]
    if att.size > MAX_BYTES:
        await message.channel.send(embed=_err_embed(
            f"That file is {_human(att.size)} — the limit is {_human(MAX_BYTES)}."))
        return
    try:
        data = await att.read()
    except discord.HTTPException:
        await message.channel.send(embed=_err_embed("I couldn't download that attachment."))
        return
    await _begin(message.author, "dm", att.filename, data, channel=message.channel)




async def create_script_cmd(ctx, file: discord.Option(discord.Attachment, description="Your .lua / .luau / .txt script")):
    if not file.filename.lower().endswith(ALLOWED_EXT):
        await ctx.respond(embed=_err_embed("Please attach a `.lua`, `.luau`, or `.txt` file."), ephemeral=True)
        return
    if file.size > MAX_BYTES:
        await ctx.respond(embed=_err_embed(f"That file is {_human(file.size)} — the limit is {_human(MAX_BYTES)}."), ephemeral=True)
        return
    data = await file.read()
    await _begin(ctx.author, "slash", file.filename, data, ctx=ctx)


async def manage_scripts_cmd(
    ctx,
    action: discord.Option(str, description="What to do", choices=["info", "update", "freeze", "unfreeze", "free", "paid", "delete"]),
    script_id: discord.Option(str, description="Your SCRIPT_ID- for this script"),
    file: discord.Option(discord.Attachment, description="New script (for update)", required=False, default=None),
    name: discord.Option(str, description="New name (for update)", required=False, default=None),
    silent: discord.Option(bool, description="Silent mode (for update)", required=False, default=False),
    fast: discord.Option(bool, description="Fast mode (for update)", required=False, default=False),
):
    await ctx.defer(ephemeral=True)
    if not _use_api():
        await ctx.respond(embed=_err_embed("Management needs the hosted API — set `OBF_BACKEND=api` and `API_URL`."), ephemeral=True)
        return
    if action in ("free", "paid"):
        payload = {"action": "set_free", "script_id": script_id, "free": action == "free"}
    else:
        payload = {"action": action, "script_id": script_id}
    if action == "update":
        if file is None:
            await ctx.respond(embed=_err_embed("Attach the new script file to update."), ephemeral=True)
            return
        if file.size > MAX_BYTES:
            await ctx.respond(embed=_err_embed(
                f"That file is {_human(file.size)} — the limit is {_human(MAX_BYTES)}."), ephemeral=True)
            return
        data = await file.read()
        opts = {"silent": silent, "fast": fast}
        if name:
            opts["name"] = name
        payload["source"] = data.decode("utf-8", "replace")
        payload["options"] = opts
    try:
        res = await _api_manage(payload)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)
        return

    if action == "info":
        e = discord.Embed(title="Script info", color=COL_IDLE,
                          description=(f"**{res.get('name')}**\n"
                                       f"```lua\n{res.get('loadstring')}\n```"))
        e.add_field(name="Project ID", value=f"`{res.get('project_id')}`", inline=False)
        e.add_field(name="Access", value="free"if res.get("free") else "key required", inline=True)
        e.add_field(name="Keys", value=str(res.get("keys", 0)), inline=True)
        e.add_field(name="Frozen", value="yes"if res.get("frozen") else "no", inline=True)
        e.set_footer(text=f"{BRAND} v{__version__}")
        await ctx.respond(embed=e, ephemeral=True)
    elif action == "update":
        e = _loader_embed(res.get("name"), res.get("loadstring"), False)
        e.title = "Script updated"
        await ctx.respond(embed=e, ephemeral=True)
    elif action in ("freeze", "unfreeze"):
        state = "frozen"if action == "freeze"else "live again"
        await ctx.respond(f"Script is now **{state}**.", ephemeral=True)
    elif action in ("free", "paid"):
        msg = ("Script is now **free** — no key required."if res.get("free")
               else "Script now **requires a key** (HWID-locked).")
        await ctx.respond(msg, ephemeral=True)
    elif action == "delete":
        await ctx.respond("Script **deleted** — its loadstring is now dead.", ephemeral=True)


@bot.slash_command(name="login", description="Log this Discord server into a website-created LUREX script", guild_ids=GUILD_IDS)
async def login_cmd(ctx, script_id: discord.Option(str, description="Your SCRIPT_ID- for this script")):
    await ctx.defer()
    if not getattr(ctx, "guild_id", None):
        await ctx.respond("Run `/login` inside the server you want to connect.")
        return
    try:
        info = await _api_manage({"action": "info", "script_id": script_id})
        ACTIVE_SCRIPT_SESSIONS[_session_key(ctx, script_id)] = {"script_id": str(script_id), "guild_id": str(ctx.guild_id), "name": info.get("name") or "script", "logged_in_at": int(time.time())}
        await ctx.respond(f"**LOGGED IN**\nThis server is now connected to **{info.get('name') or 'script'}** (`{script_id}`).")
    except Exception as exc:
        await ctx.respond(embed=_err_embed(f"Could not log this server into `{script_id}`.\n```\n{str(exc)[:350]}\n```"))


@bot.slash_command(name="logout", description="Log this Discord server out of a LUREX script", guild_ids=GUILD_IDS)
async def logout_cmd(ctx, script_id: discord.Option(str, description="Your SCRIPT_ID- for this script")):
    await ctx.defer()
    key = _session_key(ctx, script_id)
    if ACTIVE_SCRIPT_SESSIONS.pop(key, None):
        await ctx.respond(f"**LOGGED OUT**\nThis server is no longer connected to `{script_id}`.")
    else:
        await ctx.respond(f"This server is not logged in to `{script_id}`.")


@bot.slash_command(name="gkey", description="Generate, list or delete keys for your script", guild_ids=GUILD_IDS)
async def keys_cmd(
    ctx,
    action: discord.Option(str, description="What to do", choices=["generate", "list", "delete"]),
    script_id: discord.Option(str, description="Optional SCRIPT_ID-; leave blank to choose a script preview", required=False, default=None),
    amount: discord.Option(int, description="How many keys to generate", required=False, default=1),
    key: discord.Option(str, description="Key to delete", required=False, default=None),
    label: discord.Option(str, description="Optional label for generated keys", required=False, default=None),
):
    if not script_id:
        await _show_script_picker(ctx, lambda interaction, selected: keys_cmd(interaction, action, selected, amount, key, label))
        return
    if not await _require_script_login(ctx, script_id, "admin_role"):
        return
    await ctx.defer(ephemeral=True)
    if not _use_api():
        await ctx.respond(embed=_err_embed("Keys need the hosted API — set `OBF_BACKEND=api`."), ephemeral=True)
        return
    try:
        if action == "generate":
            res = await _api_manage({"action": "genkey", "script_id": script_id,
                                     "count": max(1, min(amount, 100)), "label": label})
            keys = res.get("keys", [])
            e = discord.Embed(title=f"Generated {len(keys)} key(s)", color=COL_KEY,
                              description="```\n"+ "\n".join(keys) + "\n```\nShare these with buyers to redeem.")
            e.set_footer(text=f"{BRAND} v{__version__}")
            await ctx.respond(embed=e, ephemeral=True)
        elif action == "list":
            res = await _api_manage({"action": "listkeys", "script_id": script_id})
            ks = res.get("keys", [])
            if not ks:
                await ctx.respond("No keys yet — generate some with `/gkey`.", ephemeral=True)
                return
            lines = [f"```\n{k['key']}\n``` — {'bound' if k['hwid_bound'] else 'unused'}"
                     + (f"· <@{k['discord_id']}>"if k.get("discord_id") else "") for k in ks[:40]]
            e = discord.Embed(title=f"{len(ks)} key(s)", color=COL_KEY, description="\n".join(lines))
            e.set_footer(text=f"{BRAND} v{__version__}")
            await ctx.respond(embed=e, ephemeral=True)
        elif action == "delete":
            if not key:
                await ctx.respond(embed=_err_embed("Provide the `key` to delete."), ephemeral=True)
                return
            res = await _api_manage({"action": "delkey", "script_id": script_id, "key": key})
            await ctx.respond("Key deleted."if res.get("deleted") else "Key not found.", ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)


@bot.slash_command(name="whitelist", description="Whitelist or blacklist a Discord user for your script", guild_ids=GUILD_IDS)
async def access_cmd(
    ctx,
    action: discord.Option(str, description="What to do", choices=["whitelist", "blacklist", "clear", "list"]),
    script_id: discord.Option(str, description="Optional SCRIPT_ID-; leave blank to choose a script preview", required=False, default=None),
    user: discord.Option(discord.User, description="Target user", required=False, default=None),
):
    if not script_id:
        await _show_script_picker(ctx, lambda interaction, selected: access_cmd(interaction, action, selected, user))
        return
    if not await _require_script_login(ctx, script_id, "admin_role"):
        return
    await ctx.defer()
    if not _use_api():
        await ctx.respond(embed=_err_embed("Access control needs the hosted API — set `OBF_BACKEND=api`."), ephemeral=True)
        return
    try:
        if action == "list":
            res = await _api_manage({"action": "listacl", "script_id": script_id})
            acl = res.get("acl", [])
            if not acl:
                await ctx.respond("No whitelist/blacklist entries.", ephemeral=True)
                return
            lines = [f"<@{a['discord_id']}> — {a['status']}" for a in acl]
            e = discord.Embed(title="Access list", color=COL_IDLE, description="\n".join(lines))
            await ctx.respond(embed=e, ephemeral=True)
            return
        if user is None:
            await ctx.respond("Choose a Discord `user` to whitelist, blacklist, or remove from this script.", ephemeral=True)
            return
        act = "unlist"if action == "clear"else action
        await _api_manage({"action": act, "script_id": script_id, "discord_id": str(user.id)})
        if action == "whitelist":
            info = await _api_manage({"action": "info", "script_id": script_id})
            manage_link = getattr(ctx.channel, "jump_url", None) or "this channel"
            await ctx.followup.send(
                f"<@{user.id}> **YOU HAVE BEEN WHITELISTED FOR THE SCRIPT:** `{info.get('name') or 'LUREX script'}`\n"
                f"**MANAGE YOUR SCRIPT:** {manage_link}\n"
                "**PANEL:** Use the deployed panel in this channel.", ephemeral=False)
        else:
            verb = {"blacklist": "blacklisted", "clear": "removed from the list"}[action]
            await ctx.respond(f"<@{user.id}> was {verb}.", ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)


@bot.slash_command(name="customise", description="Optionally customise LUREX for this server", guild_ids=GUILD_IDS)
async def customise_cmd(
    ctx,
    display_name: discord.Option(str, description="Server display name; only this server changes", required=False, default=None),
):
    await ctx.defer(ephemeral=True)
    if not ctx.guild:
        await ctx.respond("Run `/customise` inside a Discord server.", ephemeral=True)
        return
    if not getattr(ctx.author.guild_permissions, "manage_guild", False) and not getattr(ctx.author.guild_permissions, "administrator", False):
        await ctx.respond("You need Manage Server or Administrator permission to customise LUREX here.", ephemeral=True)
        return
    changed = []
    try:
        if display_name is not None:
            me = ctx.guild.me
            if me is None:
                raise RuntimeError("I could not find my member record in this server.")
            await me.edit(nick=display_name.strip() or None)
            changed.append("server display name")
        lines = [f"Updated: {', '.join(changed)} in {ctx.guild.name}." if changed else "No server display name was changed."]
        await ctx.respond("\n".join(lines), ephemeral=True)
    except Exception as exc:
        await ctx.respond(embed=_err_embed(f"```\n{str(exc)[:400]}\n```"), ephemeral=True)


async def script_info_cmd(
    ctx,
    script_id: discord.Option(str, description="Your SCRIPT_ID- for this script"),
):
    await ctx.defer(ephemeral=True)
    if not _use_api():
        await ctx.respond(embed=_err_embed("Script info needs the hosted API — set `OBF_BACKEND=api`."), ephemeral=True)
        return
    try:
        res = await _api_manage({"action": "info", "script_id": script_id})
        e = discord.Embed(title="Script info", color=COL_IDLE,
                          description=f"**{res.get('name') or 'script'}**\n```lua\n{res.get('loadstring') or 'No loader available'}\n```")
        e.add_field(name="Project ID", value=f"`{res.get('project_id') or 'n/a'}`", inline=False)
        e.add_field(name="Access", value="free" if res.get("free") else "key required", inline=True)
        e.add_field(name="Keys", value=str(res.get("keys", 0)), inline=True)
        e.add_field(name="Frozen", value="yes" if res.get("frozen") else "no", inline=True)
        e.set_footer(text=f"{BRAND} v{__version__}")
        await ctx.respond(embed=e, ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)


async def delete_script_cmd(
    ctx,
    script_id: discord.Option(str, description="Your SCRIPT_ID- for this script"),
):
    await ctx.defer(ephemeral=True)
    try:
        res = await _api_manage({"action": "delete", "script_id": script_id})
        await ctx.respond("Script **deleted** — its loadstring is now dead." if res.get("deleted", True) else "Script not found.", ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)


@bot.slash_command(name="delkey", description="Delete one access key from a script", guild_ids=GUILD_IDS)
async def delkey_cmd(
    ctx,
    script_id: discord.Option(str, description="Optional SCRIPT_ID-; leave blank to choose a script preview", required=False, default=None),
    key: discord.Option(str, description="The access key to delete"),
):
    if not script_id:
        await _show_script_picker(ctx, lambda interaction, selected: delkey_cmd(interaction, selected, key))
        return
    if not await _require_script_login(ctx, script_id, "admin_role"):
        return
    await ctx.defer(ephemeral=True)
    try:
        res = await _api_manage({"action": "delkey", "script_id": script_id, "key": key})
        await ctx.respond("Key deleted." if res.get("deleted") else "Key not found.", ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)


@bot.slash_command(name="blacklist", description="Blacklist a Discord user for a script", guild_ids=GUILD_IDS)
async def blacklist_cmd(
    ctx,
    script_id: discord.Option(str, description="Optional SCRIPT_ID-; leave blank to choose a script preview", required=False, default=None),
    user: discord.Option(discord.User, description="Target user"),
):
    if not script_id:
        await _show_script_picker(ctx, lambda interaction, selected: blacklist_cmd(interaction, selected, user))
        return
    if not await _require_script_login(ctx, script_id, "whitelist_role"):
        return
    await ctx.defer(ephemeral=True)
    try:
        await _api_manage({"action": "blacklist", "script_id": script_id, "discord_id": str(user.id)})
        await ctx.respond(f"<@{user.id}> was blacklisted.", ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)


@bot.slash_command(name="cmds", description="Show all LUREX commands", guild_ids=GUILD_IDS)
async def cmds_cmd(ctx):
    await ctx.defer(ephemeral=True)
    e = discord.Embed(title="LUREX commands", color=COL_IDLE, description=(
        "**Website**\nCreate scripts, configure panels, and assign server roles in the LUREX website.\n\n"
        "**Discord access**\n`/login` · `/logout` · `/gkey` · `/delkey` · `/whitelist` · `/blacklist` · `/kmassgen` · `/whitelist-role`\n\n"
        "**Help**\nUse the LUREX website for script and panel setup."
    ))
    e.set_footer(text=f"{BRAND} v{__version__}")
    await ctx.respond(embed=e, ephemeral=True)


async def server_link_cmd(ctx):
    await ctx.defer(ephemeral=True)
    invite = os.environ.get("SERVER_INVITE") or os.environ.get("DISCORD_INVITE")
    if invite:
        await ctx.respond(f"Join the LUREX server: {invite}", ephemeral=True)
    else:
        await ctx.respond(embed=_err_embed("No invite is configured. Set `SERVER_INVITE` on the bot service."), ephemeral=True)


async def setup_guide_cmd(ctx):
    await ctx.defer(ephemeral=True)
    e = discord.Embed(
        title="HOW TO SETUP LUREX SCRIPTS AND PANELS",
        color=COL_IDLE,
        description=(
            "**1. RUN `/create script`**\n"
            "**2. UPLOAD YOUR FILE**\n"
            "**3. SELECT YOUR NAME**\n"
            "**4. SELECT FREE OR PAID**\n"
            "**5. IF FREE, COPY YOUR LOADER AND YOU'RE GOOD TO GO!**\n"
            "**6. IF PAID, YOU WILL GET 3 KEYS:** `OWNER-` · `SCRIPT_ID-` · `PROJECT-`\n"
            "**7. USE `/deploy panel`** — enter your Script ID, choose any buyer roles, and set your customisation options.\n"
            "**8. USE `/setadmin`** — this controls who can update and manage your scripts.\n"
            "**9. RUN `/setwl`** — this sets your whitelist permissions.\n"
            "**10. USE `/manage scripts`** — edit your script preferences."
        )
    )
    e.set_footer(text=f"{BRAND} v{__version__}")
    await ctx.respond(embed=e, ephemeral=True)


async def _owner_discord_labels(user_id, server_id):
    user_label = "unknown user"
    server_label = "DM or unknown server"
    try:
        if user_id:
            user = await bot.fetch_user(int(user_id))
            user_label = user.global_name or user.name
    except Exception:
        pass
    try:
        if server_id:
            guild = bot.get_guild(int(server_id))
            if guild is None:
                guild = await bot.fetch_guild(int(server_id))
            if guild is not None:
                server_label = guild.name
    except Exception:
        pass
    return discord.utils.escape_markdown(str(user_label)), discord.utils.escape_markdown(str(server_label))

async def owner_view_cmd(ctx):
    await ctx.defer(ephemeral=True)
    if str(ctx.author.id) not in OWNER_VIEW_IDS:
        await ctx.respond(embed=_err_embed("You are not authorized to use `/owner view`."), ephemeral=True)
        return
    try:
        res = await _api_manage({"action": "owner_view", "actor_id": str(ctx.author.id)})
        scripts = res.get("scripts", [])
        audit = res.get("audit", [])
        lines = []
        for item in scripts[:50]:
            owner_text = str(item.get("owner") or "unknown")
            try:
                owner_meta = json.loads(owner_text)
                user_id = str(owner_meta.get("user_id") or "")
                server_id = str(owner_meta.get("server_id") or "")
                user_label, server_label = await _owner_discord_labels(user_id, server_id)
                owner_text = (f"user **{user_label}** (`{user_id or 'unknown'}`) · "
                              f"server **{server_label}** (`{server_id or 'DM'}`)")
            except Exception:
                owner_text = f"owner `{owner_text}`"
            lines.append(f"`{item.get('sid') or 'n/a'}` — **{item.get('name') or 'unnamed'}** — {owner_text}")
        if not lines:
            lines.append("No scripts recorded.")
        e = discord.Embed(title="LUREX owner audit", color=COL_IDLE, description="\n".join(lines))
        e.add_field(name="Audit events", value=str(len(audit)), inline=True)
        e.set_footer(text="Restricted owner view • do not share this response")
        await ctx.respond(embed=e, ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)

class ScriptSelectView(discord.ui.View):
    def __init__(self, author_id, scripts, callback):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.callback_fn = callback
        options = []
        for item in scripts[:25]:
            sid = str(item.get("sid") or "")
            name = str(item.get("name") or "script")
            if sid:
                options.append(discord.SelectOption(label=name[:100], value=sid[:100], description=f"{sid} · press to select"[:100]))
        if not options:
            options = [discord.SelectOption(label="No scripts found", value="none", description="Create a script first")]
        picker = discord.ui.Select(placeholder="Choose a script", options=options)
        picker.callback = self._selected
        self.add_item(picker)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This script selector belongs to its creator.", ephemeral=True)
            return False
        return True

    async def _selected(self, interaction):
        selected = interaction.data.get("values", [""])[0]
        if selected == "none":
            await interaction.response.send_message("No scripts found. Create scripts from the LUREX website first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.callback_fn(interaction, selected)
        except Exception as exc:
            await interaction.followup.send(embed=_err_embed(f"```\n{str(exc)[:400]}\n```"), ephemeral=True)
        self.stop()

async def _show_script_picker(ctx, callback):
    res = await _api_manage({"action": "list_mine", "actor_id": str(ctx.author.id)})
    scripts = res.get("scripts", [])
    if not scripts:
        await ctx.respond(embed=_err_embed("No scripts found for your Discord account. Create scripts from the LUREX website first."), ephemeral=True)
        return
    await ctx.respond(embed=discord.Embed(title="Select a LUREX script", color=COL_PROC,
                                           description="Choose a script preview. Each option shows the script name and its SCRIPT_ID-."),
                       view=ScriptSelectView(ctx.author.id, scripts, callback), ephemeral=True)

async def setadmin_cmd(
    ctx,
    owner_key: discord.Option(str, description="Your private OWNER- credential"),
    member: discord.Option(discord.Member, description="Member to authorize", required=False, default=None),
    role: discord.Option(discord.Role, description="Role to authorize", required=False, default=None),
    enabled: discord.Option(bool, description="Enable or remove access", required=False, default=True),
):
    await ctx.defer(ephemeral=True)
    if (member is None) == (role is None):
        await ctx.respond(embed=_err_embed("Choose exactly one `member` or `role`."), ephemeral=True)
        return
    subject_type, subject_id, label = (("member", str(member.id), member.mention) if member else ("role", str(role.id), role.mention))
    try:
        await _api_manage({"action": "setadmin", "owner_key": owner_key, "subject_type": subject_type,
                           "subject_id": subject_id, "enabled": enabled})
        state = "enabled" if enabled else "removed"
        await ctx.respond(f"Owner-style management access **{state}** for {label}.", ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)

async def setwl_cmd(
    ctx,
    owner_key: discord.Option(str, description="Your private OWNER- credential"),
    scope: discord.Option(str, description="Permission scope", choices=["all", "whitelist", "blacklist", "generate", "bulkgen"]),
    member: discord.Option(discord.Member, description="Member to authorize", required=False, default=None),
    role: discord.Option(discord.Role, description="Role to authorize", required=False, default=None),
    enabled: discord.Option(bool, description="Enable or remove access", required=False, default=True),
):
    await ctx.defer(ephemeral=True)
    if (member is None) == (role is None):
        await ctx.respond(embed=_err_embed("Choose exactly one `member` or `role`."), ephemeral=True)
        return
    subject_type, subject_id, label = (("member", str(member.id), member.mention) if member else ("role", str(role.id), role.mention))
    try:
        await _api_manage({"action": "setwl", "owner_key": owner_key, "subject_type": subject_type,
                           "subject_id": subject_id, "scope": scope, "enabled": enabled})
        state = "enabled" if enabled else "removed"
        await ctx.respond(f"`{scope}` permission **{state}** for {label}.", ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)

async def rmwl_cmd(
    ctx,
    owner_key: discord.Option(str, description="Your private OWNER- credential"),
    scope: discord.Option(str, description="Permission scope to revoke", choices=["all", "whitelist", "blacklist", "generate", "bulkgen"]),
    member: discord.Option(discord.Member, description="Member to revoke", required=False, default=None),
    role: discord.Option(discord.Role, description="Role to revoke", required=False, default=None),
):
    await ctx.defer(ephemeral=True)
    if (member is None) == (role is None):
        await ctx.respond(embed=_err_embed("Choose exactly one `member` or `role`."), ephemeral=True)
        return
    subject_type, subject_id, label = (("member", str(member.id), member.mention) if member else ("role", str(role.id), role.mention))
    try:
        await _api_manage({"action": "rmwl", "owner_key": owner_key, "subject_type": subject_type,
                           "subject_id": subject_id, "scope": scope})
        await ctx.respond(f"WL permission `{scope}` was revoked for {label}.", ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)

async def edit_panel_cmd(
    ctx,
    title: discord.Option(str, description="Panel title", required=False, default=None),
    description: discord.Option(str, description="Panel description", required=False, default=None),
    embed_color: discord.Option(str, description="Hex color such as #E88AA8", required=False, default=None),
    hwid_resets: discord.Option(bool, description="Show and allow HWID resets", required=False, default=True),
    show_redeem: discord.Option(bool, description="Show Redeem Key", required=False, default=True),
    show_get_script: discord.Option(bool, description="Show Get Script", required=False, default=True),
    show_hwid: discord.Option(bool, description="Show Reset HWID", required=False, default=True),
    show_buyer: discord.Option(bool, description="Show Get Buyer Role", required=False, default=True),
    show_key_info: discord.Option(bool, description="Show Key Info", required=False, default=True),
    emojis: discord.Option(bool, description="Show emojis on buttons", required=False, default=False),
):
    await ctx.defer(ephemeral=True)
    async def save_panel(interaction, script_id):
        await _api_manage({"action": "set_panel", "script_id": script_id, "actor_id": str(ctx.author.id),
                           "title": title, "desc": description, "color": embed_color,
                           "hwid_resets": hwid_resets, "show_redeem": show_redeem,
                           "show_get_script": show_get_script, "show_hwid": show_hwid,
                           "show_buyer": show_buyer, "show_key_info": show_key_info, "emojis": emojis})
        await interaction.followup.send(f"Panel settings saved for `{script_id}`. Use `/deploy panel` to post it.", ephemeral=True)
    await _show_script_picker(ctx, save_panel)

async def admin_cmd(
    ctx,
    action: discord.Option(str, description="Management action", choices=["info", "freeze", "unfreeze", "free", "paid", "keys", "generate", "whitelist", "blacklist", "clear"]),
    project_id: discord.Option(str, description="The PROJECT- ID you were delegated for"),
    user: discord.Option(discord.User, description="Target user for access actions", required=False, default=None),
    amount: discord.Option(int, description="Number of keys for generate", required=False, default=1),
    confirm: discord.Option(bool, description="Confirm this action", required=False, default=False),
):
    await ctx.defer(ephemeral=True)
    if action in ("whitelist", "blacklist", "clear") and user is None:
        await ctx.respond(embed=_err_embed("Choose a target `user` for this access action."), ephemeral=True)
        return
    if action in ("freeze", "unfreeze", "free", "paid") and not confirm:
        await ctx.respond(embed=_err_embed("Set `confirm` to true for this state-changing action."), ephemeral=True)
        return
    action_map = {"free": "set_free", "paid": "set_free", "keys": "listkeys", "generate": "genkey", "clear": "unlist"}
    api_action = action_map.get(action, action)
    payload = {"action": api_action, "project": project_id, "actor_id": str(ctx.author.id)}
    if action in ("free", "paid"):
        payload["free"] = action == "free"
    if action == "generate":
        payload["count"] = max(1, min(int(amount or 1), 100))
    if action in ("whitelist", "blacklist", "clear"):
        payload["discord_id"] = str(user.id)
    try:
        res = await _api_manage(payload)
        if action == "generate":
            keys = res.get("keys", [])
            await ctx.respond(embed=discord.Embed(title=f"Generated {len(keys)} key(s)", color=COL_KEY, description="\n".join(f"`{k}`" for k in keys)), ephemeral=True)
        elif action == "keys":
            keys = res.get("keys", [])
            await ctx.respond("\n".join(f"`{k.get('key')}`" for k in keys[:50]) or "No keys.", ephemeral=True)
        else:
            await ctx.respond(f"Delegated action `{action}` completed for `{project_id}`.", ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)

@bot.slash_command(name="kmassgen", description="Generate up to 100 access keys at once", guild_ids=GUILD_IDS)
async def kmassgen_cmd(
    ctx,
    script_id: discord.Option(str, description="Optional SCRIPT_ID-; leave blank to choose a script preview", required=False, default=None),
    amount: discord.Option(int, description="Number of keys", required=False, default=10),
    label: discord.Option(str, description="Optional label", required=False, default=None),
):
    if not script_id:
        await _show_script_picker(ctx, lambda interaction, selected: kmassgen_cmd(interaction, selected, amount, label))
        return
    if not await _require_script_login(ctx, script_id, "admin_role"):
        return
    await ctx.defer(ephemeral=True)
    try:
        res = await _api_manage({"action": "genkey", "script_id": script_id, "count": max(1, min(amount, 100)), "label": label})
        keys = res.get("keys", [])
        e = discord.Embed(title=f"Generated {len(keys)} key(s)", color=COL_KEY, description="```\n" + "\n".join(keys) + "\n```")
        e.set_footer(text=f"{BRAND} v{__version__}")
        await ctx.respond(embed=e, ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)


@bot.slash_command(name="whitelist-role", description="Whitelist every member of a Discord role", guild_ids=GUILD_IDS)
async def whitelist_role_cmd(
    ctx,
    script_id: discord.Option(str, description="Optional SCRIPT_ID-; leave blank to choose a script preview", required=False, default=None),
    role: discord.Option(discord.Role, description="Role whose members should be whitelisted"),
):
    if not script_id:
        await _show_script_picker(ctx, lambda interaction, selected: whitelist_role_cmd(interaction, selected, role))
        return
    if not await _require_script_login(ctx, script_id, "whitelist_role"):
        return
    await ctx.defer(ephemeral=True)
    members = list(role.members)
    if not members:
        await ctx.respond(f"No members found in {role.mention}.", ephemeral=True)
        return
    try:
        for member in members[:100]:
            await _api_manage({"action": "whitelist", "script_id": script_id, "discord_id": str(member.id)})
        await ctx.respond(f"Whitelisted {min(len(members), 100)} member(s) from {role.mention}.", ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)


PANEL_BLURB = ("Redeem a key, grab your loader, and manage access from one panel. "
               "Owners can choose which controls appear when deploying.")
def _panel_embed(name, title=None, desc=None, color=None, config=None):
    config = config or {}
    col = COL_PROC
    if color:
        try:
            col = int(str(color).strip().lstrip("#"), 16)
        except ValueError:
            col = COL_PROC
    luarmor = bool(config.get("panel_luarmor_look", False))
    if luarmor:
        panel_title = f"{name} Control Panel"
        panel_desc = "If you're a buyer, click on the buttons below to redeem your key, get the script or get your role."
        if desc:
            panel_desc += f"\n\n{desc}"
    else:
        panel_title = title or f"{name} access panel"
        panel_desc = desc or f"Redeem a LUREX key to access the script: **{name}**."
    e = discord.Embed(title=panel_title, description=panel_desc, color=col)
    if not luarmor:
        fields = [
            ("Redeem Key", "Bind a key to your account.", "panel_show_redeem"),
            ("Get Script", "Your ready-to-run loadstring.", "panel_show_get_script"),
            ("Reset HWID", "Reset your device binding.", "panel_show_hwid"),
            ("Get Buyer Role", "Unlock the buyer role.", "panel_show_buyer"),
            ("Key Info", "Check your key status.", "panel_show_key_info"),
        ]
        for field_name, field_value, field_key in fields:
            if config.get(field_key, True) and (field_key != "panel_show_hwid" or config.get("panel_hwid_resets", True)):
                e.add_field(name=field_name, value=field_value, inline=True)
    e.set_footer(text=f"{BRAND} • secure script access")
    return e

class RedeemModal(discord.ui.Modal):
    def __init__(self, project):
        super().__init__(title="Redeem your LUREX key")
        self.project = project
        self.key_input = discord.ui.InputText(label="Key", placeholder="LUREX-123-456-789", required=True)
        self.add_item(self.key_input)
    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        key = (self.key_input.value or "").strip()
        try:
            await _api_manage({"action": "redeem", "project": self.project,
                               "discord_id": str(interaction.user.id), "key": key})
        except Exception as e:
            await interaction.followup.send(embed=_err_embed(f"```\n{str(e)[:300]}\n```"), ephemeral=True)
            return
        await interaction.followup.send("Key redeemed. Use **Get Script** to receive your loader.", ephemeral=True)

class PanelView(discord.ui.View):
    def __init__(self, project, role_id, config=None):
        super().__init__(timeout=None)
        self.project = project
        self.role_id = role_id
        self.config = config or {}
        self._configure_buttons()
        actions = {"redeem": "redeem", "get_script": "get_script", "reset_hwid": "reset_hwid", "buyer_role": "buyer_role", "key_info": "key_info"}
        for child in self.children:
            callback = getattr(child.callback, "func", child.callback)
            name = getattr(callback, "__name__", "")
            action = actions.get(name)
            if action:
                suffix = f":{self.role_id}" if action == "buyer_role" else ""
                child.custom_id = f"lurexpanel:{action}:{self.project}{suffix}"[:100]
    def _button_label(self, emoji, text):
        return f"{emoji} {text}" if (self.config.get("panel_emojis") or self.config.get("panel_luarmor_look")) else text
    def _configure_buttons(self):
        visible = {
            "redeem": self.config.get("panel_show_redeem", True),
            "get_script": self.config.get("panel_show_get_script", True),
            "reset_hwid": self.config.get("panel_show_hwid", True) and self.config.get("panel_hwid_resets", True),
            "buyer_role": self.config.get("panel_show_buyer", True),
            "key_info": self.config.get("panel_show_key_info", True),
        }
        luarmor = bool(self.config.get("panel_luarmor_look", False))
        labels = {"redeem": ("🔑", "Redeem Key"), "get_script": ("📜", "Get Script"),
                  "reset_hwid": ("⚙️", "Reset HWID"), "buyer_role": ("👤", "Get Role" if luarmor else "Get Buyer Role"),
                  "key_info": ("📊", "Get Stats" if luarmor else "Key Info")}
        for child in list(self.children):
            callback = getattr(child.callback, "func", child.callback)
            name = getattr(callback, "__name__", "")
            if name in visible:
                if not visible[name]:
                    self.remove_item(child)
                else:
                    child.label = self._button_label(*labels[name])
                    if luarmor:
                        child.style = {"redeem": discord.ButtonStyle.success, "get_script": discord.ButtonStyle.primary, "buyer_role": discord.ButtonStyle.primary}.get(name, discord.ButtonStyle.secondary)
    async def _act(self, interaction, action, extra=None):
        payload = {"action": action, "project": self.project, "discord_id": str(interaction.user.id)}
        if extra:
            payload.update(extra)
        return await _api_manage(payload)
    @discord.ui.button(label="Redeem Key", style=discord.ButtonStyle.primary)
    async def redeem(self, a, b):
        interaction = _interaction(a, b)
        await interaction.response.send_modal(RedeemModal(self.project))
    @discord.ui.button(label="Get Script", style=discord.ButtonStyle.success)
    async def get_script(self, a, b):
        interaction = _interaction(a, b)
        await interaction.response.defer(ephemeral=True)
        try:
            res = await self._act(interaction, "get_script")
        except Exception as e:
            await interaction.followup.send(embed=_err_embed(f"```\n{str(e)[:300]}\n```"), ephemeral=True)
            return
        e = discord.Embed(title="Your loader", color=COL_OK,
                          description="Copy the value below into your executor:\n" + _loader_lines(res["loadstring"]))
        e.set_footer(text=f"{BRAND} v{__version__}")
        await interaction.followup.send(embed=e, ephemeral=True)
    @discord.ui.button(label="Reset HWID", style=discord.ButtonStyle.secondary)
    async def reset_hwid(self, a, b):
        interaction = _interaction(a, b)
        await interaction.response.defer(ephemeral=True)
        try:
            await self._act(interaction, "reset_hwid")
        except Exception as e:
            await interaction.followup.send(embed=_err_embed(f"```\n{str(e)[:300]}\n```"), ephemeral=True)
            return
        await interaction.followup.send("HWID reset — your key rebinds on next launch.", ephemeral=True)
    @discord.ui.button(label="Get Buyer Role", style=discord.ButtonStyle.secondary)
    async def buyer_role(self, a, b):
        interaction = _interaction(a, b)
        await interaction.response.defer(ephemeral=True)
        try:
            res = await self._act(interaction, "buyer_check")
        except Exception as e:
            await interaction.followup.send(embed=_err_embed(f"```\n{str(e)[:300]}\n```"), ephemeral=True)
            return
        if not res.get("eligible"):
            await interaction.followup.send("Redeem a valid key or ask the owner to whitelist you first.", ephemeral=True)
            return
        guild = interaction.guild
        role = guild.get_role(self.role_id) if guild else None
        if role is None:
            await interaction.followup.send("Buyer role not found in this server.", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="LUREX: verified buyer")
        except discord.Forbidden:
            await interaction.followup.send("I can't assign that role — move my role above it and grant Manage Roles.", ephemeral=True)
            return
        await interaction.followup.send(f"You now have **{role.name}**.", ephemeral=True)
    @discord.ui.button(label="Key Info", style=discord.ButtonStyle.secondary)
    async def key_info(self, a, b):
        interaction = _interaction(a, b)
        await interaction.response.defer(ephemeral=True)
        try:
            res = await self._act(interaction, "key_info")
        except Exception as e:
            await interaction.followup.send(embed=_err_embed(f"```\n{str(e)[:300]}\n```"), ephemeral=True)
            return
        if not res.get("redeemed"):
            txt = "This script is free or you are whitelisted — no key needed." if res.get("free") else "You have not redeemed a key yet."
            await interaction.followup.send(txt, ephemeral=True)
            return
        reset_in = res.get("reset_in", 0)
        reset_txt = "available now" if reset_in == 0 else f"in {reset_in // 3600}h {(reset_in % 3600) // 60}m"
        e = discord.Embed(title="Your key", color=COL_KEY,
                          description=_copy_block("KEY", res.get("key")) +
                                      f"\nHWID: {'bound' if res.get('hwid_bound') else 'not bound'}\nHWID reset: {reset_txt}")
        e.set_footer(text=f"{BRAND} v{__version__}")
        await interaction.followup.send(embed=e, ephemeral=True)
@bot.listen("on_interaction")
async def _handle_website_panel_interaction(interaction):
    data = getattr(interaction, "data", None) or {}
    custom_id = data.get("custom_id")
    # Native `lurexpanel:` messages are handled by PanelView callbacks. Handling
    # them again here races the callback and can make Discord report that the
    # interaction failed or was already acknowledged. The global listener is
    # reserved for website-created `lurex:` components.
    if not isinstance(custom_id, str) or not custom_id.startswith("lurex:"):
        return
    parts = custom_id.split(":")
    if len(parts) < 3:
        return
    prefix, action, project = parts[:3]
    role_id = parts[3] if len(parts) > 3 else None
    if action == "redeem":
        await interaction.response.send_modal(RedeemModal(project))
        return
    if action not in {"get_script", "reset_hwid", "buyer_role", "key_info"}:
        return
    await interaction.response.defer(ephemeral=True)
    try:
        api_action = "buyer_check" if action == "buyer_role" else action
        result = await _api_manage({"action": api_action, "project": project, "discord_id": str(interaction.user.id)})
        if action == "reset_hwid":
            await interaction.followup.send("HWID reset — your key will rebind on next launch.", ephemeral=True)
            return
        if action == "buyer_role":
            if not result.get("eligible"):
                await interaction.followup.send("Redeem a valid key or ask the owner to whitelist you first.", ephemeral=True)
                return
            role = interaction.guild.get_role(int(role_id)) if role_id and getattr(interaction, "guild", None) else None
            if role is not None:
                try:
                    await interaction.user.add_roles(role, reason="LUREX: verified buyer")
                    await interaction.followup.send(f"You now have **{role.name}**.", ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send("I can't assign that role — move my role above it and grant Manage Roles.", ephemeral=True)
            else:
                await interaction.followup.send("Buyer access verified for this script. Ask the server owner to assign the buyer role if needed.", ephemeral=True)
            return
        if action == "get_script":
            embed = discord.Embed(title="Your loader", color=COL_OK, description="Copy the value below into your executor:\n" + _loader_lines(result["loadstring"]))
        elif not result.get("redeemed"):
            text = "This script is free or you are whitelisted — no key needed." if result.get("free") else "You have not redeemed a key yet."
            await interaction.followup.send(text, ephemeral=True)
            return
        else:
            reset_in = result.get("reset_in", 0)
            reset_txt = "available now" if reset_in == 0 else f"in {reset_in // 3600}h {(reset_in % 3600) // 60}m"
            embed = discord.Embed(title="Your key", color=COL_KEY, description=_copy_block("KEY", result.get("key")) + f"\nHWID: {'bound' if result.get('hwid_bound') else 'not bound'}\nHWID reset: {reset_txt}")
        embed.set_footer(text=f"{BRAND} v{__version__}")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(embed=_err_embed(f"```\n{str(exc)[:300]}\n```"), ephemeral=True)


async def deploy_panel_cmd(
    ctx,
    script_id: discord.Option(str, description="Your SCRIPT_ID- for this script"),
    buyer_role: discord.Option(discord.Role, description="Role to grant verified buyers"),
    title: discord.Option(str, description="Panel title", required=False, default=None),
    description: discord.Option(str, description="Panel description", required=False, default=None),
    embed_color: discord.Option(str, description="Hex color such as #E88AA8", required=False, default=None),
    hwid_resets: discord.Option(bool, description="Show and allow HWID resets", required=False, default=True),
    show_redeem: discord.Option(bool, description="Show Redeem Key", required=False, default=True),
    show_get_script: discord.Option(bool, description="Show Get Script", required=False, default=True),
    show_hwid: discord.Option(bool, description="Show Reset HWID", required=False, default=True),
    show_buyer: discord.Option(bool, description="Show Get Buyer Role", required=False, default=True),
    show_key_info: discord.Option(bool, description="Show Key Info", required=False, default=True),
    emojis: discord.Option(bool, description="Show emojis on buttons", required=False, default=False),
    luarmor_look: discord.Option(bool, description="Use the Luarmor-style visual panel", required=False, default=False),
):
    await ctx.defer(ephemeral=True)
    if not _use_api():
        await ctx.respond(embed=_err_embed("Panels need the hosted API — set `OBF_BACKEND=api`."), ephemeral=True)
        return
    try:
        actor_id = str(ctx.author.id)
        info = await _api_manage({"action": "info", "script_id": script_id, "actor_id": actor_id})
        project_id = info.get("project_id")
        if not project_id:
            raise RuntimeError("The selected script has no PROJECT- reference.")
        await _api_manage({"action": "set_panel", "script_id": script_id, "actor_id": actor_id,
                            "title": title, "desc": description, "color": embed_color,
                            "hwid_resets": hwid_resets, "show_redeem": show_redeem,
                            "show_get_script": show_get_script, "show_hwid": show_hwid,
                            "show_buyer": show_buyer, "show_key_info": show_key_info,
                            "emojis": emojis, "luarmor_look": luarmor_look})
        info = await _api_manage({"action": "panel_info", "project": project_id})
        await ctx.channel.send(
            embed=_panel_embed(info.get("name") or "script", info.get("panel_title"),
                               info.get("panel_desc"), info.get("panel_color"), info),
            view=PanelView(project_id, buyer_role.id, info))
        await ctx.respond("Custom control panel posted.", ephemeral=True)
    except Exception as e:
        await ctx.respond(embed=_err_embed(f"```\n{str(e)[:400]}\n```"), ephemeral=True)
def main():
    if not TOKEN:
        print("Set TOKEN (or DISCORD_TOKEN) in the environment before running.", file=sys.stderr)
        raise SystemExit(2)
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
