/*
 * mod-jevstate — see JevState.h.
 *
 * All AzerothCore APIs used here were verified against master
 * (azerothcore/azerothcore-wotlk) on 2026-09-18:
 *   WorldScript::OnUpdate/OnStartup/OnShutdown/OnAfterConfigLoad
 *   sWorldSessionMgr->GetAllSessions(), WorldSession::GetPlayer()
 *   Player::GetSelectedUnit(), GetPet(), GetSpellCooldownMap(),
 *       getPowerType(), GetPower/GetMaxPower, IsInCombat(), isMoving()
 *   Unit::getAttackers(), GetVictim(), GetLevel(), getClass(), GetName()
 *   Unit::GetCurrentSpell(CURRENT_GENERIC_SPELL), Spell::GetCastTimeRemaining()
 *   Unit::GetAppliedAuras(), AuraApplication::GetBase()/IsPositive()/GetStackAmount()
 *   Aura::GetDuration()/GetMaxDuration()
 *   ThreatManager::GetThreat(Unit const*)
 *   sSpellMgr->GetSpellInfo(id)->SpellName[DEFAULT_LOCALE]
 */

#include "JevState.h"

#include "Common.h"
#include "Config.h"
#include "Group.h"
#include "Log.h"
#include "Player.h"
#include "Pet.h"
#include "ScriptMgr.h"
#include "Spell.h"
#include "SpellAuras.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "ObjectAccessor.h"
#include "ObjectGuid.h"
#include "WorldSession.h"
#include "WorldSessionMgr.h"
#include "GameTime.h"

// Navigation: PathGenerator is the same mmaps/Recast API creatures use
// (verified vs master 2026-09-25: Movement/MovementGenerators/PathGenerator.h,
// class PathGenerator(WorldObject const*), CalculatePath(x,y,z,forceDest),
// GetPath() -> Movement::PointsArray, GetPathType(), PATHFIND_* in PathType).
#include "PathGenerator.h"

// Inventory digest: Bag::GetBagSize()/GetItemByPos(uint8) (Entities/Item/
// Container/Bag.h) and Player::GetBagByPos/GetItemByPos with
// INVENTORY_SLOT_BAG_0=255, bag slots 19..22, backpack 23..38
// (verified vs master 2026-09-25).
#include "Bag.h"
#include "Item.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <set>

namespace {

// ---- JSON helpers ---------------------------------------------------------

void JsonEscape(std::string& out, std::string const& s)
{
    for (char c : s)
    {
        switch (c)
        {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (static_cast<unsigned char>(c) < 0x20)
                {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                }
                else
                    out += c;
        }
    }
}

std::string J(std::string const& s) { return "\"" + s + "\""; }
std::string JQ(std::string const& s) { std::string q = "\""; JsonEscape(q, s); return q + "\""; }

std::string Num1(double v) { char b[32]; std::snprintf(b, sizeof(b), "%.1f", v); return b; }
std::string Num2(double v) { char b[32]; std::snprintf(b, sizeof(b), "%.2f", v); return b; }

// Free inventory slots: backpack (23..38 via INVENTORY_SLOT_BAG_0) + every
// equipped bag's slots. The grind loop's "go vendor" threshold reads this.
uint32_t CountFreeBagSlots(Player* player)
{
    uint32_t free = 0;
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        if (!player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
            ++free;
    for (uint8 bag = INVENTORY_SLOT_BAG_START; bag < INVENTORY_SLOT_BAG_END; ++bag)
        if (Bag* bagPtr = player->GetBagByPos(bag))
            for (uint32 slot = 0; slot < bagPtr->GetBagSize(); ++slot)
                if (!bagPtr->GetItemByPos(static_cast<uint8>(slot)))
                    ++free;
    return free;
}

// Worst durability percent across equipped items that HAVE durability.
// 100.0 when nothing equipped has the stat. The repair threshold reads this.
double EquippedDurabilityPct(Player* player)
{
    bool any = false;
    double worst = 100.0;
    for (uint8 slot = EQUIPMENT_SLOT_START; slot < EQUIPMENT_SLOT_END; ++slot)
    {
        Item* item = player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot);
        if (!item)
            continue;
        uint32 maxDur = item->GetUInt32Value(ITEM_FIELD_MAXDURABILITY);
        if (maxDur == 0)
            continue;
        double pct = 100.0 * item->GetUInt32Value(ITEM_FIELD_DURABILITY) / maxDur;
        if (pct < worst)
            worst = pct;
        any = true;
    }
    return any ? worst : 100.0;
}

// PathType bit flags -> wire status (order matters: worst first).
char const* PathTypeName(PathType t)
{
    if (t & PATHFIND_NOPATH)         return "nopath";
    if (t & PATHFIND_INCOMPLETE)     return "incomplete";
    if (t & PATHFIND_SHORTCUT)       return "shortcut";
    if (t & PATHFIND_NOT_USING_PATH) return "not_using_path";
    if (t & PATHFIND_SHORT)          return "short";
    if (t & PATHFIND_FARFROMPOLY)    return "farfrompoly";
    if (t & PATHFIND_NORMAL)         return "normal";
    return "blank";
}

char const* PowerName(int32 power)
{
    switch (power)
    {
        case 0: return "mana";
        case 1: return "rage";
        case 2: return "focus";
        case 3: return "energy";
        case 4: return "happiness";
        case 6: return "runic_power";
        default: return "other";
    }
}

char const* ClassName(uint8 cls)
{
    switch (cls)
    {
        case 1: return "Warrior";
        case 2: return "Paladin";
        case 3: return "Hunter";
        case 4: return "Rogue";
        case 5: return "Priest";
        case 6: return "Death Knight";
        case 7: return "Shaman";
        case 8: return "Mage";
        case 9: return "Warlock";
        case 11: return "Druid";
        default: return "Unknown";
    }
}

std::string SpellNameOf(uint32 spellId)
{
    SpellInfo const* si = sSpellMgr->GetSpellInfo(spellId);
    if (!si || !si->SpellName[DEFAULT_LOCALE])
        return "";
    return si->SpellName[DEFAULT_LOCALE];
}

// ---- per-unit pieces ------------------------------------------------------

void AppendCasting(std::string& out, Unit* u)
{
    Spell* spell = u->GetCurrentSpell(CURRENT_GENERIC_SPELL);
    if (!spell || spell->GetCastTimeRemaining() <= 0)
    {
        out += "null";
        return;
    }
    SpellInfo const* si = spell->GetSpellInfo();
    out += "{\"spell\":" + JQ(si ? SpellNameOf(si->Id) : std::string("?")) +
           ",\"remain_ms\":" + std::to_string(spell->GetCastTimeRemaining()) +
           ",\"interruptible\":" + ((si && (si->InterruptFlags & SPELL_INTERRUPT_FLAG_INTERRUPT)) ? "true" : "false") +
           "}";
}

void AppendAuras(std::string& out, Unit* u, bool harmfulOnly, uint32_t cap)
{
    auto const& auras = u->GetAppliedAuras(); // member typedef; deduce it
    uint32_t n = 0;
    out += "[";
    for (auto itr = auras.begin(); itr != auras.end() && n < cap; ++itr)
    {
        AuraApplication* app = itr->second;
        if (!app)
            continue;
        bool harmful = !app->IsPositive();
        if (harmfulOnly != harmful)
            continue;
        Aura* base = app->GetBase();
        if (!base)
            continue;
        if (n++)
            out += ",";
        bool permanent = base->GetMaxDuration() == -1;
        out += "{\"name\":" + JQ(SpellNameOf(itr->first)) +
               ",\"id\":" + std::to_string(itr->first) +
               ",\"remain_ms\":" + (permanent ? std::string("0") : std::to_string(base->GetDuration())) +
               ",\"permanent\":" + (permanent ? "true" : "false") +
               ",\"harmful\":" + (harmful ? "true" : "false") +
               ",\"stacks\":" + std::to_string(base->GetStackAmount()) +
               "}";
    }
    out += "]";
}

} // namespace

// ---- JevState -------------------------------------------------------------

JevState& JevState::Instance()
{
    static JevState instance;
    return instance;
}

void JevState::LoadConfig()
{
    _enabled = sConfigMgr->GetOption<bool>("JevState.Enable", true);
    _bindIp = sConfigMgr->GetOption<std::string>("JevState.BindIP", "0.0.0.0");
    _port = sConfigMgr->GetOption<uint16_t>("JevState.Port", 7879);
    _updateMs = sConfigMgr->GetOption<uint32_t>("JevState.UpdateMs", 100);
    _maxAttackers = sConfigMgr->GetOption<uint32_t>("JevState.MaxAttackers", 8);
    _attackerRangeYards = sConfigMgr->GetOption<float>("JevState.AttackerRangeYards", 45.0f);
}

void JevState::UpdateSnapshots(uint32_t /*nowMs*/)
{
    std::map<std::string, std::string> fresh;
    uint64_t const frame = ++_frame;
    int64_t const nowMs = GameTime::GetGameTimeMS().count();

    for (auto const& [id, session] : sWorldSessionMgr->GetAllSessions())
    {
        (void)id;
        Player* player = session ? session->GetPlayer() : nullptr;
        if (!player || !player->IsInWorld())
            continue;

        std::string out = "{\"frame\":" + std::to_string(frame) +
                          ",\"t_ms\":" + std::to_string(nowMs) +
                          ",\"player\":{";

        // --- player core ---
        {
            uint32 hp = player->GetHealth(), maxHp = player->GetMaxHealth();
            int32 powerType = player->getPowerType();
            uint32 pw = player->GetPower(static_cast<Powers>(powerType));
            uint32 pwMax = player->GetMaxPower(static_cast<Powers>(powerType));
            out += "\"name\":" + JQ(player->GetName()) +
                   ",\"class\":" + JQ(ClassName(player->getClass())) +
                   ",\"level\":" + std::to_string(player->GetLevel()) +
                   ",\"hp\":" + std::to_string(hp) +
                   ",\"max_hp\":" + std::to_string(maxHp) +
                   ",\"hp_pct\":" + Num1(maxHp ? 100.0 * hp / maxHp : 0.0) +
                   ",\"power\":{\"type\":" + JQ(PowerName(powerType)) +
                   ",\"cur\":" + std::to_string(pw) +
                   ",\"max\":" + std::to_string(pwMax) +
                   ",\"pct\":" + Num1(pwMax ? 100.0 * pw / pwMax : 0.0) + "}" +
                   ",\"in_combat\":" + (player->IsInCombat() ? "true" : "false") +
                   ",\"moving\":" + (player->isMoving() ? "true" : "false") +
                   ",\"auto_attacking\":" +
                   ((player->GetCurrentSpell(CURRENT_MELEE_SPELL) ||
                     player->GetCurrentSpell(CURRENT_AUTOREPEAT_SPELL)) ? "true" : "false") +
                   ",\"x\":" + Num1(player->GetPositionX()) +
                   ",\"y\":" + Num1(player->GetPositionY()) +
                   ",\"z\":" + Num1(player->GetPositionZ()) +
                   ",\"o\":" + Num2(player->GetOrientation()) +
                   ",\"map\":" + std::to_string(player->GetMapId()) +
                   ",\"zone\":" + std::to_string(player->GetZoneId()) +
                   ",\"gold\":" + std::to_string(player->GetMoney()) +
                   ",\"bag_free\":" + std::to_string(CountFreeBagSlots(player)) +
                   ",\"durability_pct\":" + Num1(EquippedDurabilityPct(player));
        }

        // --- pet ---
        {
            Pet* pet = player->GetPet();
            if (pet && pet->IsInWorld() && pet->IsAlive())
            {
                uint32 hp = pet->GetHealth(), maxHp = pet->GetMaxHealth();
                out += ",\"pet\":{\"name\":" + JQ(pet->GetName()) +
                       ",\"hp_pct\":" + Num1(maxHp ? 100.0 * hp / maxHp : 0.0) + "}";
            }
            else
                out += ",\"pet\":null";
        }

        // --- target ---
        {
            Unit* target = player->GetSelectedUnit();
            if (target)
            {
                uint32 hp = target->GetHealth(), maxHp = target->GetMaxHealth();
                out += ",\"target\":{\"name\":" + JQ(target->GetName()) +
                       ",\"level\":" + std::to_string(target->GetLevel()) +
                       ",\"hp_pct\":" + Num1(maxHp ? 100.0 * hp / maxHp : 0.0) +
                       ",\"is_player\":" + (target->GetTypeId() == TYPEID_PLAYER ? "true" : "false") +
                       ",\"distance_yd\":" + Num1(player->GetDistance(target)) +
                       ",\"my_threat\":" + Num1(target->GetThreatMgr().GetThreat(player)) +
                       ",\"casting\":";
                AppendCasting(out, target);
                out += ",\"auras\":";
                AppendAuras(out, target, /*harmfulOnly=*/true, 12);
                out += "}";
            }
            else
                out += ",\"target\":null";
        }

        // --- the fight: attackers of the player UNION attackers of the pet
        // UNION the hostile current target, GUID-deduped. ---
        // player->getAttackers() alone is literally "units attacking the
        // player" — with the pet tanking a large pull (the normal hunter
        // shape) most mobs live in the PET's AttackerSet and were invisible,
        // so the loop's min_attackers gates (Multi-Shot/Volley at 3) never
        // opened. The union is what the player experiences as "the fight".
        {
            Unit* pet = player->GetPet();
            Unit* target = player->GetSelectedUnit();
            std::set<ObjectGuid> seen;
            out += ",\"attackers\":[";
            uint32_t n = 0;
            auto emit = [&](Unit* attacker)
            {
                if (n >= _maxAttackers)
                    return;
                if (!attacker || !attacker->IsInWorld() || !attacker->IsAlive())
                    return;
                if (!attacker->IsInMap(player))
                    return;
                if (attacker == static_cast<Unit*>(player) || (pet && attacker == pet))
                    return;
                if (!seen.insert(attacker->GetGUID()).second)
                    return;
                if (player->GetDistance(attacker) > _attackerRangeYards)
                    return;
                if (n++)
                    out += ",";
                uint32 hp = attacker->GetHealth(), maxHp = attacker->GetMaxHealth();
                Unit* victim = attacker->GetVictim();
                out += "{\"name\":" + JQ(attacker->GetName()) +
                       ",\"level\":" + std::to_string(attacker->GetLevel()) +
                       ",\"hp_pct\":" + Num1(maxHp ? 100.0 * hp / maxHp : 0.0) +
                       ",\"is_player\":" + (attacker->GetTypeId() == TYPEID_PLAYER ? "true" : "false") +
                       ",\"distance_yd\":" + Num1(player->GetDistance(attacker)) +
                       ",\"attacking_me\":" + (victim == static_cast<Unit*>(player) ? "true" : "false") +
                       ",\"attacking_pet\":" + (pet && victim == static_cast<Unit*>(pet) ? "true" : "false") +
                       ",\"attacking_ally\":" + (victim && victim->GetTypeId() == TYPEID_PLAYER && victim != static_cast<Unit*>(player) ? "true" : "false") +
                       ",\"casting\":";
                AppendCasting(out, attacker);
                out += "}";
            };
            for (Unit* attacker : player->getAttackers())
                emit(attacker);
            if (pet)
                for (Unit* attacker : pet->getAttackers())
                    emit(attacker);
            // Group content: a friend (or their pet) tanking keeps mobs in
            // THEIR AttackerSet — union those in too. Range/map gating keeps
            // a member's distant fight out of ours.
            if (Group* group = player->GetGroup())
            {
                for (Group::MemberSlot const& slot : group->GetMemberSlots())
                {
                    Player* member = ObjectAccessor::FindConnectedPlayer(slot.guid);
                    if (!member || member == player || !member->IsInWorld() || !member->IsInMap(player))
                        continue;
                    for (Unit* attacker : member->getAttackers())
                        emit(attacker);
                    if (Pet* memberPet = member->GetPet())
                        for (Unit* attacker : memberPet->getAttackers())
                            emit(attacker);
                }
            }
            if (target && target->IsHostileTo(player))
                emit(target);
            out += "]";
        }

        // --- spell cooldowns (remaining > 0 only, deduped by name) ---
        // SpellCooldown.end is in GAME-TIME MILLISECONDS (Player::_AddSpellCooldown:
        // sc.end = GameTime::GetGameTimeMS().count() + end_time, with end_time in
        // ms — verified vs master 2026-09-19; mixing it with seconds produced
        // absurd remain_ms values). The map is keyed by spellId, so one press
        // creates entries for every rank/category-linked spell: dedupe by
        // NAME keeping the max remaining, or 11x "Raptor Strike" crowds the cap.
        {
            std::map<std::string, int64_t> byName;
            for (auto const& [spellId, cd] : player->GetSpellCooldownMap())
            {
                if (cd.itemid)
                    continue; // item-driven cooldown, not an ability
                int64_t remainMs = int64_t(cd.end) - nowMs;
                if (remainMs <= 0)
                    continue;
                std::string name = SpellNameOf(spellId);
                int64_t& slot = byName[name];
                if (remainMs > slot)
                    slot = remainMs;
            }
            out += ",\"cooldowns\":[";
            uint32_t n = 0;
            for (auto const& [name, remainMs] : byName)
            {
                if (n++)
                    out += ",";
                out += "{\"spell\":" + JQ(name) +
                       ",\"remain_ms\":" + std::to_string(remainMs) + "}";
                if (n >= 40)
                    break;
            }
            out += "]";
        }

        // --- own auras ---
        out += ",\"auras\":";
        AppendAuras(out, player, /*harmfulOnly=*/false, 24);

        out += "}}"; // player, root

        std::string key = player->GetName();
        std::string lower;
        lower.reserve(key.size());
        for (char c : key)
            lower += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        fresh[std::move(lower)] = std::move(out);
    }

    std::lock_guard<std::mutex> lock(_cacheMutex);
    _cache = std::move(fresh);
}

std::string JevState::GetSnapshot(std::string const& name) const
{
    std::string lower;
    lower.reserve(name.size());
    for (char c : name)
        lower += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    std::lock_guard<std::mutex> lock(_cacheMutex);
    auto itr = _cache.find(lower);
    return itr != _cache.end() ? itr->second : std::string();
}

std::string JevState::HealthJson() const
{
    std::lock_guard<std::mutex> lock(_cacheMutex);
    return "{\"ok\":true,\"players\":" + std::to_string(_cache.size()) +
           ",\"frame\":" + std::to_string(_frame) + "}";
}

// ---- nav path (HTTP thread <-> world thread) --------------------------------

namespace {

// Runs on the WORLD thread: all map/navmesh context is valid here.
std::string ComputeNavPath(JevNavRequest const& r)
{
    Player* player = ObjectAccessor::FindPlayerByName(r.name);
    if (!player || !player->IsInWorld())
        return "{\"status\":\"error\",\"error\":\"player offline or not in world\"}";

    auto t0 = std::chrono::steady_clock::now();
    PathGenerator pathGen(player);
    pathGen.CalculatePath(r.x, r.y, r.z, r.forceDest);
    double ms = std::chrono::duration<double, std::milli>(
                    std::chrono::steady_clock::now() - t0).count();

    Movement::PointsArray const& pts = pathGen.GetPath();
    std::string out = "{\"status\":" + JQ(PathTypeName(pathGen.GetPathType())) +
                      ",\"length_yd\":" + Num1(pathGen.getPathLength()) +
                      ",\"points\":[";
    uint32_t n = 0;
    for (G3D::Vector3 const& p : pts)
    {
        if (n++)
            out += ",";
        out += "[" + Num2(p.x) + "," + Num2(p.y) + "," + Num2(p.z) + "]";
    }
    out += "],\"elapsed_ms\":" + Num1(ms) + "}";
    return out;
}

// GET query "?x=1.5&y=-2&z=3&force=1" -> coords. Returns false if any of
// x/y/z is missing or not a finite number.
bool ParseNavQuery(std::string const& query, float& x, float& y, float& z, bool& forceDest)
{
    x = y = z = 0.f;
    forceDest = false;
    bool haveX = false, haveY = false, haveZ = false;
    size_t pos = 0;
    while (pos < query.size())
    {
        size_t amp = query.find('&', pos);
        if (amp == std::string::npos)
            amp = query.size();
        std::string kv = query.substr(pos, amp - pos);
        pos = amp + 1;
        size_t eq = kv.find('=');
        if (eq == std::string::npos)
            continue;
        std::string k = kv.substr(0, eq);
        std::string v = kv.substr(eq + 1);
        if (k == "force")
        {
            forceDest = (v == "1" || v == "true");
            continue;
        }
        char* end = nullptr;
        float f = std::strtof(v.c_str(), &end);
        if (!end || *end != '\0' || !std::isfinite(f))
            continue;
        if (k == "x") { x = f; haveX = true; }
        else if (k == "y") { y = f; haveY = true; }
        else if (k == "z") { z = f; haveZ = true; }
    }
    return haveX && haveY && haveZ;
}

} // namespace

void JevState::ProcessNavRequests()
{
    std::vector<JevNavRequest> reqs;
    {
        std::lock_guard<std::mutex> lock(_navMutex);
        if (_navPending.empty())
            return;
        reqs.swap(_navPending);
        // purge unconsumed results (client timed out / disconnected)
        int64_t nowMs = GameTime::GetGameTimeMS().count();
        for (auto itr = _navResults.begin(); itr != _navResults.end();)
            if (itr->second.second < nowMs)
                itr = _navResults.erase(itr);
            else
                ++itr;
    }
    for (JevNavRequest const& r : reqs)
    {
        std::string json = ComputeNavPath(r);
        {
            std::lock_guard<std::mutex> lock(_navMutex);
            _navResults[r.id] = {std::move(json),
                                 GameTime::GetGameTimeMS().count() + 30000};
        }
    }
    _navCv.notify_all();
}

std::string JevState::RequestNavPath(std::string const& name,
                                     float x, float y, float z, bool forceDest)
{
    uint64_t id;
    {
        std::lock_guard<std::mutex> lock(_navMutex);
        id = _navNextId++;
        _navPending.push_back({id, name, x, y, z, forceDest});
    }
    std::unique_lock<std::mutex> lock(_navMutex);
    // A world tick is ~50-100ms and a detour query is µs-ms, so 2s covers
    // even a busy world thread; on timeout the request is simply dropped.
    bool done = _navCv.wait_for(lock, std::chrono::seconds(2),
        [this, id] { return _navResults.find(id) != _navResults.end(); });
    if (!done)
        return "{\"status\":\"timeout\",\"error\":\"world thread did not answer within 2s\"}";
    std::string json = std::move(_navResults[id].first);
    _navResults.erase(id);
    return json;
}

// ---- HTTP -----------------------------------------------------------------

bool JevState::StartHttp()
{
    if (_threadStarted)
        return true;
    if (!_enabled)
    {
        LOG_INFO("module", "mod-jevstate: disabled (JevState.Enable=0)");
        return true;
    }

    _listenFd = socket(AF_INET, SOCK_STREAM, 0);
    if (_listenFd < 0)
    {
        LOG_ERROR("module", "mod-jevstate: socket() failed: {}", std::strerror(errno));
        return false;
    }
    int one = 1;
    setsockopt(_listenFd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(_port);
    addr.sin_addr.s_addr = inet_addr(_bindIp.c_str());
    if (bind(_listenFd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0)
    {
        LOG_ERROR("module", "mod-jevstate: bind({}:{}) failed: {}", _bindIp, _port, std::strerror(errno));
        close(_listenFd);
        _listenFd = -1;
        return false;
    }
    if (listen(_listenFd, 8) < 0)
    {
        LOG_ERROR("module", "mod-jevstate: listen() failed: {}", std::strerror(errno));
        close(_listenFd);
        _listenFd = -1;
        return false;
    }

    _running = true;
    _threadStarted = true;
    _httpThread = std::thread([this] { AcceptLoop(); });
    LOG_INFO("module", "mod-jevstate: serving on {}:{}, snapshots every {}ms",
             _bindIp, _port, _updateMs);
    return true;
}

void JevState::StopHttp()
{
    if (!_threadStarted)
        return;
    _running = false;
    if (_listenFd >= 0)
    {
        shutdown(_listenFd, SHUT_RDWR);
        close(_listenFd);
        _listenFd = -1;
    }
    if (_httpThread.joinable())
        _httpThread.join();
    _threadStarted = false;
}

void JevState::AcceptLoop()
{
    while (_running)
    {
        sockaddr_in peer{};
        socklen_t peerLen = sizeof(peer);
        int fd = accept(_listenFd, reinterpret_cast<sockaddr*>(&peer), &peerLen);
        if (fd < 0)
        {
            if (!_running)
                break;
            if (errno == EINTR)
                continue;
            LOG_ERROR("module", "mod-jevstate: accept() failed: {}", std::strerror(errno));
            continue;
        }

        // read the request head (we only need the first line)
        std::string req;
        char buf[1024];
        while (req.find("\r\n\r\n") == std::string::npos && req.size() < 4096)
        {
            ssize_t n = recv(fd, buf, sizeof(buf), 0);
            if (n <= 0)
                break;
            req.append(buf, n);
        }

        std::string status = "404 Not Found";
        std::string body = "{\"error\":\"not found\"}";
        size_t sp1 = req.find(' ');
        size_t sp2 = req.find(' ', sp1 + 1);
        if (req.rfind("GET ", 0) == 0 && sp1 != std::string::npos && sp2 != std::string::npos)
        {
            std::string target = req.substr(sp1 + 1, sp2 - sp1 - 1);
            size_t qm = target.find('?');
            std::string path = (qm == std::string::npos) ? target : target.substr(0, qm);
            std::string query = (qm == std::string::npos) ? std::string() : target.substr(qm + 1);
            if (path == "/health")
            {
                status = "200 OK";
                body = HealthJson();
            }
            else if (path.rfind("/state/", 0) == 0)
            {
                std::string name = path.substr(7);
                body = GetSnapshot(name);
                if (body.empty())
                {
                    status = "404 Not Found";
                    body = "{\"error\":\"no snapshot for that character (offline or not yet cached)\"}";
                }
                else
                    status = "200 OK";
            }
            else if (path.rfind("/nav/path/", 0) == 0)
            {
                std::string name = path.substr(10);
                float x, y, z;
                bool forceDest;
                if (name.empty() || !ParseNavQuery(query, x, y, z, forceDest))
                {
                    status = "400 Bad Request";
                    body = "{\"status\":\"error\",\"error\":\"usage: /nav/path/<Name>?x=<float>&y=<float>&z=<float>[&force=1]\"}";
                }
                else
                {
                    status = "200 OK";
                    body = RequestNavPath(name, x, y, z, forceDest);
                }
            }
        }

        std::string resp = "HTTP/1.0 " + status + "\r\n"
                           "Content-Type: application/json\r\n"
                           "Content-Length: " + std::to_string(body.size()) + "\r\n"
                           "Connection: close\r\n\r\n" + body;
        size_t sent = 0;
        while (sent < resp.size())
        {
            ssize_t n = send(fd, resp.data() + sent, resp.size() - sent, MSG_NOSIGNAL);
            if (n <= 0)
                break;
            sent += static_cast<size_t>(n);
        }
        close(fd);
    }
}

// ---- script registration --------------------------------------------------

class JevStateWorldScript : public WorldScript
{
public:
    JevStateWorldScript() : WorldScript("mod_jevstate") { sJevState.LoadConfig(); }

    void OnAfterConfigLoad(bool /*reload*/) override { sJevState.LoadConfig(); }

    void OnStartup() override { sJevState.StartHttp(); }
    void OnShutdown() override { sJevState.StopHttp(); }

    void OnUpdate(uint32_t /*diff*/) override
    {
        // Nav path requests are rare and latency-sensitive: service them on
        // every world tick, ahead of the snapshot cadence gate below.
        sJevState.ProcessNavRequests();

        uint32_t const nowMs = static_cast<uint32_t>(GameTime::GetGameTimeMS().count());
        if (nowMs - _last < sJevState.UpdateMs()) // uint32 wrap-safe comparison
            return;
        _last = nowMs;
        sJevState.UpdateSnapshots(nowMs);
    }

private:
    uint32_t _last = 0;
};

void AddJevStateScripts()
{
    new JevStateWorldScript();
}
