from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from mods_base import ENGINE, BoolOption, build_mod, get_pc, hook
from networking import add_network_functions
from networking.decorators import host, targeted
from unrealsdk import find_enum, find_object, logging
from unrealsdk.hooks import Type, add_hook, remove_hook
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer, WrappedStruct

if TYPE_CHECKING:
    from common import WillowGameEngine, WillowPlayerController, WillowPlayerPawn, WorldInfo

SLIDE_SPEED_DEFAULT: float = 2
CROUCHED_PCT_DEFAULT: float = 0.5
FALLING_MOVE: str = "WillowGame.WillowPlayerController:PlayerFalling.PlayerMove"
FALLING_HOOK_ID: str = "slide:jump-carry"
HUD_HOOK_ID: str = "slide:icon"

_CLIP_PREFIXES: tuple[str, ...] = ("p1.", "p2.", "p3.", "p4.", "")
_HUD_HOOK_CANDIDATES: tuple[str, ...] = (
    "WillowGame.WillowHUD:PostRender",
    "GearboxFramework.GearboxHUD:PostRender",
    "Engine.HUD:PostRender",
    "GFxUI.GFxMoviePlayer:PostAdvance",
)

_installed_hud_hooks: list[str] = []
_clip_prefix: str | None = None
_missing_clip_logged: bool = False
_hud_error_logged: bool = False


class State:
    do_slide_jump: ClassVar[bool] = False
    jump_started: ClassVar[bool] = False
    jump_still_grounded: ClassVar[bool] = False
    carrying_jump: ClassVar[bool] = False
    tweener: ClassVar[Any] = None
    vel_x: ClassVar[float] = 0.0
    vel_y: ClassVar[float] = 0.0
    jump_crouch_pct: ClassVar[float] = SLIDE_SPEED_DEFAULT
    can_chain_slide: ClassVar[bool] = False
    chain_until: ClassVar[float] = 0.0
    chain_speed: ClassVar[float] = SLIDE_SPEED_DEFAULT
    crouch_held: ClassVar[bool] = False
    saw_air: ClassVar[bool] = False
    fell: ClassVar[bool] = False
    jump_at: ClassVar[float] = 0.0
    keep_duck: ClassVar[bool] = False
    carry_until: ClassVar[float] = 0.0


CHAIN_WINDOW: float = 1.0


@dataclass
class PlayerSlideState:
    old_z: float
    is_sliding: bool


CLIENTS_SLIDE_STATES: dict[WeakPointer[WillowPlayerController], PlayerSlideState] = {}
OWN_SLIDE_STATE: PlayerSlideState = PlayerSlideState(old_z=0, is_sliding=False)
e_net_mode: WorldInfo.ENetMode = cast("WorldInfo.ENetMode", find_enum("ENetMode"))

_last_server_tick: float | None = None
_missing_tweens_logged: bool = False
_falling_hooked: bool = False


dip_weapon = BoolOption(
    identifier="Dip Weapon",
    value=True,
    description="Tilt the first-person weapon down for the length of the slide.",
)


def is_client() -> bool:
    return cast("WillowGameEngine", ENGINE).GetCurrentWorldInfo().NetMode == e_net_mode.NM_Client


def world_time() -> float:
    return float(cast("WillowGameEngine", ENGINE).GetCurrentWorldInfo().TimeSeconds)


def jump_carry_active() -> bool:
    return State.do_slide_jump or State.carrying_jump


def next_slide_speed(speed: float, z_diff: float, delta_time: float) -> float:
    """Slow the slide over time. Downhill (negative z) gives a little speed back."""
    if z_diff < 0:
        return speed - z_diff * 0.0005
    return speed - (delta_time * 0.7 + z_diff * 0.007)


def _function_exists(hook_path: str) -> bool:
    class_path, func_name = hook_path.split(":", 1)
    try:
        return find_object("Function", f"{class_path}.{func_name}") is not None
    except Exception:
        return False


def _tween_module() -> Any | None:
    global _missing_tweens_logged
    try:
        import tweens
    except ImportError:
        if not _missing_tweens_logged:
            _missing_tweens_logged = True
            logging.warning("Slide: tweens is not installed, so the weapon will not dip.")
        return None
    return tweens


def _play_arm_tween(pc: WillowPlayerController, sliding: bool) -> None:
    if not dip_weapon.value:
        return
    tweens = _tween_module()
    if tweens is None:
        return
    pawn = pc.Pawn
    if pawn is None:
        return
    arms = pawn.Arms
    if arms is None or not arms.Attachments or arms.SkeletalMesh is None:
        return

    if State.tweener is not None and State.tweener.is_running():
        State.tweener.kill()

    mesh = arms.SkeletalMesh
    tween = tweens.Tween()
    State.tweener = tween
    # Same pose as juso's Sliding mod: pitch the gun down and drop it toward the slide.
    if sliding:
        keys = (
            (mesh.RotOrigin, "Pitch", 500, 0.2, tweens.cubic_in_out),
            (mesh.RotOrigin, "Yaw", -200, 0.4, tweens.quad_out),
            (mesh.RotOrigin, "Roll", -6300, 0.5, tweens.cubic_out),
            (mesh.Origin, "X", 30, 1.2, tweens.elastic_out),
            (mesh.Origin, "Y", -14.5, 0.5, tweens.circ_out),
            (mesh.Origin, "Z", -175, 0.5, tweens.circ_out),
        )
    else:
        keys = (
            (mesh.RotOrigin, "Pitch", 0, 0.5, tweens.cubic_in_out),
            (mesh.RotOrigin, "Yaw", 0, 0.4, tweens.quad_out),
            (mesh.RotOrigin, "Roll", 0, 0.3, tweens.cubic_in_out),
            (mesh.Origin, "X", 40, 0.4, tweens.circ_out),
            (mesh.Origin, "Y", 0, 0.6, tweens.circ_out),
            (mesh.Origin, "Z", -167, 0.5, tweens.circ_out),
        )
    for target, prop, final_value, duration, transition in keys:
        tween.tween_property(
            target,
            prop,
            final_value=final_value,
            duration=duration,
        ).from_current().transition(transition)
    tween.set_parallel(True)
    tween.start()


def _track_server_slide(pc: WillowPlayerController, *, sliding: bool) -> None:
    """Record a slide on the host. Solo and listen-server games are the host."""
    pawn = pc.Pawn
    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player, None)
        elif _pc == pc:
            data = CLIENTS_SLIDE_STATES[player]
            data.is_sliding = sliding
            if sliding and pawn is not None:
                data.old_z = pawn.Location.Z
            break
    else:
        if sliding and pawn is not None:
            CLIENTS_SLIDE_STATES[WeakPointer(pc)] = PlayerSlideState(
                old_z=pawn.Location.Z,
                is_sliding=True,
            )
    if pawn is None:
        return
    pawn.CrouchedPct = SLIDE_SPEED_DEFAULT if sliding else CROUCHED_PCT_DEFAULT


@host.json_message
def server_set_slide_jump_velocity(vel_x: float, vel_y: float) -> None:
    pc = cast("WillowPlayerController", server_set_slide_jump_velocity.sender.Owner)
    if pc.Pawn is None:
        return
    pc.Pawn.Velocity.X = vel_x
    pc.Pawn.Velocity.Y = vel_y


@host.message
def server_exit_slide() -> None:
    pc = cast("WillowPlayerController", server_exit_slide.sender.Owner)
    _track_server_slide(pc, sliding=False)


def _clear_jump_carry() -> None:
    State.do_slide_jump = False
    State.jump_started = False
    State.jump_still_grounded = False
    State.carrying_jump = False
    State.saw_air = False
    State.fell = False
    State.jump_at = 0.0
    State.carry_until = 0.0


def _clear_slide_chain() -> None:
    State.can_chain_slide = False
    State.chain_until = 0.0


def _chain_window_open() -> bool:
    if not State.can_chain_slide:
        return False
    if world_time() > State.chain_until:
        _clear_slide_chain()
        return False
    return True


def enter_slide(pc: WillowPlayerController, speed: float | None = None) -> None:
    """Start the local slide, and tell the host when this machine is a client."""
    if OWN_SLIDE_STATE.is_sliding or pc.Pawn is None:
        return
    slide_speed = SLIDE_SPEED_DEFAULT if speed is None else speed
    # Standalone Borderlands 2 is the host. Applying it here keeps the slide
    # working when there is no party leader to send a network message to.
    if is_client():
        server_enter_slide()
    else:
        _track_server_slide(pc, sliding=True)
    OWN_SLIDE_STATE.is_sliding = True
    OWN_SLIDE_STATE.old_z = pc.Pawn.Location.Z
    pc.Pawn.CrouchedPct = slide_speed
    _play_arm_tween(pc, sliding=True)


def _arm_slide_chain(duration: float = CHAIN_WINDOW) -> None:
    """After a slide is cancelled with a jump, the next crouch can slide again."""
    State.can_chain_slide = True
    State.chain_speed = max(State.jump_crouch_pct, SLIDE_SPEED_DEFAULT)
    until = world_time() + duration
    if until > State.chain_until:
        State.chain_until = until


def _start_chained_slide(pc: WillowPlayerController) -> bool:
    if not _chain_window_open() or pc.Pawn is None:
        return False
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    # Crouch during the jump is remembered. The slide starts on the landing frame.
    if jump_carry_active() and not _jump_has_landed(pawn):
        State.crouch_held = True
        return False
    speed = State.chain_speed
    _clear_slide_chain()
    pc.bDuck = True
    enter_slide(pc, speed)
    return OWN_SLIDE_STATE.is_sliding


def _finish_local_slide(pc: WillowPlayerController, *, notify_server: bool) -> None:
    if not OWN_SLIDE_STATE.is_sliding:
        return
    OWN_SLIDE_STATE.is_sliding = False
    if pc.Pawn is not None:
        pc.Pawn.CrouchedPct = CROUCHED_PCT_DEFAULT
    # Clients report the exit. On the host, the slide record is updated here.
    if notify_server:
        if is_client():
            server_exit_slide()
        else:
            _track_server_slide(pc, sliding=False)
    _play_arm_tween(pc, sliding=False)


def exit_slide(pc: WillowPlayerController) -> None:
    _clear_jump_carry()
    _finish_local_slide(pc, notify_server=True)


@targeted.message
def client_exit_slide() -> None:
    pc = _local_pc()
    if pc is not None:
        exit_slide(pc)


@host.message
def server_enter_slide() -> None:
    pc = cast("WillowPlayerController", server_enter_slide.sender.Owner)
    if pc.Pawn is None:
        return
    _track_server_slide(pc, sliding=True)


def update_slide_speed(
    pc: WillowPlayerController,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> None:
    pawn = pc.Pawn
    if pawn is None:
        return
    z_diff = float(pawn.Location.Z) - slide_data.old_z
    speed = next_slide_speed(float(pawn.CrouchedPct), z_diff, delta_time)
    slide_data.old_z = pawn.Location.Z
    pawn.CrouchedPct = speed


def still_sliding(pc: WillowPlayerController, pawn: WillowPlayerPawn) -> bool:
    accel = pawn.Acceleration
    return bool(pc.bDuck) and pawn.IsOnGroundOrShortFall() and not (
        accel.X == 0.0 and accel.Y == 0.0
    )


def _local_pc() -> WillowPlayerController | None:
    try:
        return cast("WillowPlayerController | None", get_pc())
    except Exception:
        return None


def _hiding(pc: WillowPlayerController) -> bool:
    """True for a normal crouch and for a slide, which is also a crouch."""
    if bool(pc.bDuck):
        return True
    pawn = pc.Pawn
    if pawn is None:
        return False
    try:
        return float(pawn.CrouchedPct) > CROUCHED_PCT_DEFAULT + 0.05
    except Exception:
        return False


def _hud_movie(pc: WillowPlayerController) -> UObject | None:
    try:
        return cast("UObject | None", pc.GetHUDMovie())
    except Exception:
        return None


def _clip_prefix_for(movie: UObject) -> str | None:
    global _clip_prefix, _missing_clip_logged
    if _clip_prefix is not None:
        return _clip_prefix
    for prefix in _CLIP_PREFIXES:
        try:
            clip = movie.GetVariableObject(prefix + "crouch")
        except Exception:
            clip = None
        if clip is not None:
            _clip_prefix = prefix
            return prefix
    if not _missing_clip_logged:
        _missing_clip_logged = True
        logging.warning("Slide: could not find the crouch icon.")
    return None


def _hide_icon(pc: WillowPlayerController) -> None:
    global _hud_error_logged
    if not _hiding(pc):
        return
    movie = _hud_movie(pc)
    if movie is None:
        return
    prefix = _clip_prefix_for(movie)
    if prefix is None:
        return
    path = prefix + "crouch"
    try:
        movie.SetVariableBool(path + "._visible", False)
        movie.SetVariableNumber(path + "._alpha", 0)
    except Exception:
        if not _hud_error_logged:
            _hud_error_logged = True
            logging.warning("Slide: failed to hide the crouch icon.")


def _after_hud(
    obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    pc = _local_pc()
    if pc is None or not _hiding(pc):
        return
    movie = _hud_movie(pc)
    hud = getattr(pc, "myHUD", None)
    if obj != movie and obj != hud:
        return
    _hide_icon(pc)


def _install_hud_hooks() -> None:
    _remove_hud_hooks()
    for path in _HUD_HOOK_CANDIDATES:
        if not _function_exists(path):
            continue
        add_hook(path, Type.POST, HUD_HOOK_ID, _after_hud)
        _installed_hud_hooks.append(path)


def _remove_hud_hooks() -> None:
    while _installed_hud_hooks:
        path = _installed_hud_hooks.pop()
        try:
            remove_hook(path, Type.POST, HUD_HOOK_ID)
        except Exception:
            pass


def _same_player(left: WillowPlayerController, right: WillowPlayerController) -> bool:
    if left == right:
        return True
    try:
        return left.PlayerReplicationInfo == right.PlayerReplicationInfo
    except Exception:
        return False


def _tell_client_to_exit(pc: WillowPlayerController) -> None:
    local = _local_pc()
    if local is not None and _same_player(pc, local):
        _finish_local_slide(pc, notify_server=False)
        return
    pri = pc.PlayerReplicationInfo
    if pri is not None:
        client_exit_slide(pri)


def _should_end_slide(pc: WillowPlayerController, pawn: WillowPlayerPawn) -> bool:
    return not still_sliding(pc, pawn) or float(pawn.CrouchedPct) < CROUCHED_PCT_DEFAULT


def _handle_local_slide_update(pc: WillowPlayerController, pawn: WillowPlayerPawn, delta_time: float) -> None:
    if not OWN_SLIDE_STATE.is_sliding or jump_carry_active():
        return
    if _should_end_slide(pc, pawn):
        exit_slide(pc)
        return
    update_slide_speed(pc, OWN_SLIDE_STATE, delta_time)
    if float(pawn.CrouchedPct) < CROUCHED_PCT_DEFAULT:
        exit_slide(pc)


def _handle_jump_carry_move(pc: WillowPlayerController, pawn: WillowPlayerPawn) -> None:
    if not jump_carry_active():
        return
    if _jump_has_landed(pawn):
        _finish_jump_carry(pc, pawn, landed=True)
    elif not State.saw_air:
        pc.bDuck = False
        _launch_slide_jump(pc, pawn)
    elif State.carry_until and world_time() > State.carry_until:
        _finish_jump_carry(pc, pawn, landed=bool(pawn.IsOnGroundOrShortFall()))


def _handle_chained_slide_check(pc: WillowPlayerController) -> None:
    local = _local_pc()
    if local is not None and _same_player(pc, local) and _chain_window_open() and bool(pc.bDuck):
        _start_chained_slide(pc)


def server_tick_slides(delta_time: float) -> None:
    """Decay every sliding player once per frame, including clients while the host is standing."""
    global _last_server_tick
    now = world_time()
    if _last_server_tick == now:
        return
    _last_server_tick = now

    local = _local_pc() if jump_carry_active() else None
    for player in CLIENTS_SLIDE_STATES.copy():
        pc = player()
        if pc is None or pc.Pawn is None:
            CLIENTS_SLIDE_STATES.pop(player, None)
            continue
        # Leave the jumper's slide speed alone until they land.
        if local is not None and _same_player(pc, local):
            continue
        data = CLIENTS_SLIDE_STATES[player]
        if not data.is_sliding:
            continue
        pawn = cast("WillowPlayerPawn", pc.Pawn)
        if _should_end_slide(pc, pawn):
            data.is_sliding = False
            pawn.CrouchedPct = CROUCHED_PCT_DEFAULT
            _tell_client_to_exit(pc)
            continue
        update_slide_speed(pc, data, delta_time)
        if float(pawn.CrouchedPct) < CROUCHED_PCT_DEFAULT:
            data.is_sliding = False
            pawn.CrouchedPct = CROUCHED_PCT_DEFAULT
            _tell_client_to_exit(pc)


def _clear_pressed_jump(pc: WillowPlayerController) -> None:
    try:
        pc.bPressedJump = False
    except Exception:
        pass


def _stand_for_jump(pc: WillowPlayerController, pawn: WillowPlayerPawn) -> None:
    """Ask the game to stand up. Clearing bIsCrouched directly skips UnCrouch and sticks the capsule."""
    pc.bDuck = False
    try:
        pawn.ShouldCrouch(False)
    except Exception:
        try:
            pawn.bWantsToCrouch = False
        except Exception:
            pass


def _launch_slide_jump(pc: WillowPlayerController, pawn: WillowPlayerPawn) -> None:
    """Stand out of the slide and jump, keeping the slide's horizontal speed."""
    _stand_for_jump(pc, pawn)
    try:
        crouched = bool(pawn.bIsCrouched)
    except Exception:
        crouched = False
    try:
        pc.bPressedJump = True
    except Exception:
        pass
    if not crouched:
        try:
            pawn.DoJump(True)
        except Exception:
            pass
        try:
            jump_z = float(pawn.JumpZ)
        except Exception:
            jump_z = 632.0
        if float(pawn.Velocity.Z) < jump_z * 0.5:
            pawn.Velocity.Z = jump_z
    pawn.Velocity.X = State.vel_x
    pawn.Velocity.Y = State.vel_y
    _clear_pressed_jump(pc)
    if is_client():
        server_set_slide_jump_velocity(State.vel_x, State.vel_y)


def _apply_jump_velocity(pc: WillowPlayerController, pawn: WillowPlayerPawn) -> None:
    """Keep the slide speed while the jump is in the air."""
    if is_client():
        server_set_slide_jump_velocity(State.vel_x, State.vel_y)
    pawn.Velocity.X = State.vel_x
    pawn.Velocity.Y = State.vel_y


def _physics_is(pawn: WillowPlayerPawn, name: str) -> bool:
    try:
        return pawn.Physics == getattr(find_enum("EPhysics"), name)
    except Exception:
        return False


def _standing_on_something(pawn: WillowPlayerPawn) -> bool:
    try:
        if pawn.Base is not None:
            return True
    except Exception:
        pass
    if _physics_is(pawn, "PHYS_Walking"):
        return True
    try:
        return str(pawn.Physics).endswith("PHYS_Walking")
    except Exception:
        return False


def _jump_has_landed(pawn: WillowPlayerPawn) -> bool:
    """True on the first frame the game puts you back on the ground."""
    z = float(pawn.Velocity.Z)
    if z < -30.0:
        State.fell = True
    if not State.saw_air or z > 150.0:
        return False
    if _standing_on_something(pawn):
        return True
    if not State.fell or z < -60.0:
        return False
    try:
        return bool(pawn.IsOnGroundOrShortFall())
    except Exception:
        return abs(z) < 40.0


def _finish_jump_carry(pc: WillowPlayerController, pawn: WillowPlayerPawn | None, *, landed: bool) -> None:
    """Stop the jump carry. Crouch on landing starts the next slide immediately."""
    held = bool(pc.bDuck) or State.crouch_held if landed else False
    _clear_jump_carry()
    State.crouch_held = False
    _clear_pressed_jump(pc)
    if not landed or pawn is None:
        return
    _arm_slide_chain()
    if held:
        pc.bDuck = True
        _start_chained_slide(pc)


def _carry_jump_speed(pc: WillowPlayerController) -> None:
    local = _local_pc()
    if local is None or not _same_player(pc, local) or not jump_carry_active():
        return
    pawn = cast("WillowPlayerPawn | None", pc.Pawn)
    if pawn is None:
        _clear_jump_carry()
        return
    z = float(pawn.Velocity.Z)
    try:
        on_ground = bool(pawn.IsOnGroundOrShortFall())
    except Exception:
        on_ground = False
    if z > 150.0 or not on_ground:
        State.saw_air = True
        State.carrying_jump = True
        State.jump_still_grounded = False
        if z < -30.0:
            State.fell = True
        _apply_jump_velocity(pc, pawn)
    if _jump_has_landed(pawn) or (State.carry_until and world_time() > State.carry_until and on_ground):
        _finish_jump_carry(pc, pawn, landed=True)


def _on_enable() -> None:
    global _falling_hooked
    if not _falling_hooked and _function_exists(FALLING_MOVE):
        add_hook(FALLING_MOVE, Type.POST, FALLING_HOOK_ID, _on_falling_move)
        _falling_hooked = True
    _install_hud_hooks()


def _on_disable() -> None:
    global _falling_hooked
    if _falling_hooked:
        try:
            remove_hook(FALLING_MOVE, Type.POST, FALLING_HOOK_ID)
        except Exception:
            pass
        _falling_hooked = False
    _remove_hud_hooks()
    pc = _local_pc()
    if pc is not None and OWN_SLIDE_STATE.is_sliding:
        exit_slide(pc)
    OWN_SLIDE_STATE.is_sliding = False
    _clear_jump_carry()
    _clear_slide_chain()


@hook("WillowGame.WillowPlayerInput:Jump")
def jump(
    obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    if not OWN_SLIDE_STATE.is_sliding:
        return
    pc = cast("WillowPlayerController", obj.Outer)
    pawn = pc.Pawn
    if pawn is None:
        return
    # Captured before the jump stands the player up and crouch speed replaces it.
    # Ending the slide here is the cancel. The jump keeps the speed, and landing
    # can start another slide without sprinting again.
    State.vel_x = float(pawn.Velocity.X)
    State.vel_y = float(pawn.Velocity.Y)
    State.jump_crouch_pct = max(float(pawn.CrouchedPct), CROUCHED_PCT_DEFAULT)
    State.do_slide_jump = True
    State.jump_started = True
    State.jump_still_grounded = True
    State.carrying_jump = False
    State.saw_air = False
    State.fell = False
    State.jump_at = world_time()
    State.crouch_held = False
    State.carry_until = world_time() + 0.9
    _finish_local_slide(pc, notify_server=True)
    _arm_slide_chain(2.0)
    _launch_slide_jump(pc, pawn)


@hook("WillowGame.WillowPlayerInput:Jump", Type.POST)
def jump_carry(
    obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    if not State.do_slide_jump:
        return
    pc = cast("WillowPlayerController", obj.Outer)
    pawn = cast("WillowPlayerPawn | None", pc.Pawn)
    if pawn is None:
        return
    if float(pawn.Velocity.Z) > 150.0:
        State.saw_air = True
        State.carrying_jump = True
        _apply_jump_velocity(pc, pawn)
        _clear_pressed_jump(pc)
    elif State.jump_still_grounded:
        _launch_slide_jump(pc, pawn)
        State.jump_still_grounded = False


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove")
def handle_move(
    obj: UObject,
    args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    pc = cast("WillowPlayerController", obj)
    pawn = cast("WillowPlayerPawn | None", pc.Pawn)
    if pawn is None:
        return

    local_mover = _local_pc()
    if local_mover is not None and _same_player(pc, local_mover):
        _handle_jump_carry_move(pc, pawn)

    if is_client():
        _handle_local_slide_update(pc, pawn, float(args.DeltaTime))
    else:
        server_tick_slides(float(args.DeltaTime))

    _handle_chained_slide_check(pc)


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove", Type.POST)
def handle_move_after(
    obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    _carry_jump_speed(cast("WillowPlayerController", obj))


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove", Type.POST)
def handle_move_after_icon(
    obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    pc = cast("WillowPlayerController", obj)
    local = _local_pc()
    if local is None:
        return
    if pc != local:
        try:
            if pc.PlayerReplicationInfo != local.PlayerReplicationInfo:
                return
        except Exception:
            return
    _hide_icon(pc)


def _on_falling_move(
    obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    _carry_jump_speed(cast("WillowPlayerController", obj))


@hook("WillowGame.WillowPlayerInput:DuckPressed")
def handle_duck(
    obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    pc = cast("WillowPlayerController", obj.Outer)
    State.keep_duck = False
    if State.can_chain_slide and _start_chained_slide(pc):
        State.keep_duck = True
        return
    if jump_carry_active():
        # Pressed mid-air. The slide starts the frame you land.
        State.crouch_held = True
        return
    if pc.bInSprintState:
        enter_slide(pc)
        State.keep_duck = OWN_SLIDE_STATE.is_sliding


@hook("WillowGame.WillowPlayerInput:DuckPressed", Type.POST)
def handle_duck_after(
    obj: UObject,
    _args: WrappedStruct,
    _ret: Any,
    _func: BoundFunction,
) -> None:
    # With toggle crouch, the game flips bDuck after this press. If the press
    # started a slide, a flip back to standing would end it and cost a second press.
    if not State.keep_duck:
        return
    State.keep_duck = False
    pc = cast("WillowPlayerController", obj.Outer)
    if OWN_SLIDE_STATE.is_sliding and not pc.bDuck:
        pc.bDuck = True


mod = build_mod(
    hooks=[
        handle_move,
        handle_move_after,
        handle_move_after_icon,
        handle_duck,
        handle_duck_after,
        jump,
        jump_carry,
    ],
    options=[dip_weapon],
    on_enable=_on_enable,
    on_disable=_on_disable,
)

add_network_functions(mod)
