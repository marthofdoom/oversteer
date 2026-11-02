-- Countersteer FFB telemetry sender (Countersteer, the Oversteer fork).
-- Sends the steering rack force and each tyre's load and slip to Oversteer over UDP on this computer, at up to
-- 500 Hz, in a small fixed binary packet ("CSB1", 96 bytes, little endian; the layout is repeated in
-- oversteer/telemetry_formats.py). Nothing here changes the vehicle. Only the player's vehicle runs the physics-step
-- hook (it is enabled once that vehicle is seated by the player, main.lua:526); the setup is in a pcall, the step
-- body is in one pcall (an error is logged once and the sender goes quiet), and a failed send (Oversteer not
-- running) is only backed off from.
--
-- Optional /settings/countersteer_ffb.json in the user folder: {"port": 5320, "hz": 500}.
--
-- Every API used was found in BeamNG.drive 0.40.4's own Lua (paths below are under lua/vehicle/):
--   enablePhysicsStepHook()                               main.lua:65 (as extensions/test/ffbCalibration.lua:264 calls it)
--   onPhysicsStep(dtPhys), onReset, onPlayersChanged      main.lua:83, :454, :526 (hooks); playerInfo.firstPlayerSeated main.lua:507
--   hydros.hydros, h.inputSource, h.bcid, h.beamCID,      hydros.lua:32, :668 (the source is renamed to
--     h.inputFactor, h.fIsBroken                          "steering_input"), :618, :324, :354
--   obj:getBeamStress(bcid) * h.inputFactor               hydros.lua:324 (the game's own rack stress, in N)
--   electrics.values.steering_input                       hydros.lua:297, :759
--   obj:getGroundSpeed()                                  electrics.lua:416
--   obj:getSimTime()                                      mapmgr.lua:180
--   wheels.wheels[cid] (.name .lastSlip .lastSideSlip     wheels.lua:1109, :374 (set at the graphics rate:
--     .slipEnergy .downForceRaw)                          getSlipVelEnergyDownPeakForceDepthMats returns
--                                                         slip, sideSlip, slipEnergy, downForce, peakForce, ...)
--   wheel names F/R + L/R prefix convention               input.lua:132
--   socket.udp(), settimeout, setpeername, send           protocols.lua:157, extensions/debug/advancedExternalDebug.lua:115-117
--   ffi.cdef / ffi.new / ffi.sizeof / ffi.string          protocols.lua:5, :163-170
--   jsonReadFile(path)                                    common/utils.lua:484
--   obj:getPhysicsDt()                                    main.lua:55 (the global physicsDt)

local M = {}

local MOD_VERSION = 1 -- read by Oversteer's installer: never overwrites a newer one. Bump with every change.

local ffi = require("ffi")

local DEFAULT_PORT = 5320
local DEFAULT_HZ = 500
local HOST = "127.0.0.1"
local BACKOFF = 20 -- after a failed send, try again this many send intervals later

pcall(ffi.cdef, [[
typedef struct {float down, slip, sideSlip, slipEnergy;} csb1_wheel;
typedef struct {
  uint32_t magic, seq;
  float time, rack, steer, speed;
  uint32_t mask;
  float hz;
  csb1_wheel w[4];
} csb1_packet;
]])

local packet
local packetSize
local udp
local port, hz = DEFAULT_PORT, DEFAULT_HZ
local ready = false
local stepsPerSend = 1
local stepCount = 0
local sendWait = 0
local seq = 0
local rackSum = 0
local rackSteps = 0
local rackHydros = {} -- {bcid, factor, isBroken}, flat arrays so the step allocates nothing
local rackCount = 0
local wheelSlots = {} -- [1..4] = wheel data (FL, FR, RL, RR) or false
local mask = 0
local hooked = false

local function setupSocket()
  udp = nil
  if not socket then return end
  local sock = socket.udp()
  if not sock then return end
  sock:settimeout(0)
  local ok = sock:setpeername(HOST, port)
  if ok then udp = sock end
end

local function setupSlots()
  -- FL FR RL RR by the game's own name convention (input.lua:132); a wheel it does not place takes a free slot
  table.clear(wheelSlots)
  for i = 1, 4 do wheelSlots[i] = false end
  local rest = {}
  if wheels and wheels.wheels then
    for _, wd in pairs(wheels.wheels) do
      if wd.name and wd.rotatorType == "wheel" then
        local name = wd.name
        local long, side = string.match(name, "^([FR][FR0-9_]*)([RL][RL0-9]*)")
        local slot
        if long and side then
          slot = (long:sub(1, 1) == "F" and 0 or 2) + (side:sub(1, 1) == "L" and 1 or 2)
        end
        if slot and not wheelSlots[slot] then
          wheelSlots[slot] = wd
        else
          table.insert(rest, wd)
        end
      end
    end
  end
  for _, wd in ipairs(rest) do
    for i = 1, 4 do
      if not wheelSlots[i] then
        wheelSlots[i] = wd
        break
      end
    end
  end
  mask = 0
  for i = 1, 4 do
    if wheelSlots[i] then mask = mask + 2 ^ (i - 1) end
  end
end

local function setupHydros()
  -- the steering hydros that are beams (a torsion-bar hydro has no beam stress)
  rackCount = 0
  if hydros and hydros.hydros then
    for _, h in pairs(hydros.hydros) do
      if (h.inputSource == "steering_input" or h.inputSource == "steering") and h.beamCID and h.bcid then
        rackCount = rackCount + 1
        rackHydros[rackCount] = h
      end
    end
  end
end

local function setup()
  ready = true
  setupHydros()
  setupSlots()
  local dt = physicsDt or 0.0005
  stepsPerSend = math.max(1, math.floor(1 / (hz * dt) + 0.5))
  stepCount, sendWait, rackSum, rackSteps = 0, 0, 0, 0
end

local function send()
  local p = packet
  seq = seq + 1
  p.magic = 0x31425343 -- "CSB1" as bytes
  p.seq = seq
  p.time = obj:getSimTime()
  p.rack = rackSteps > 0 and rackSum / rackSteps or 0
  p.steer = electrics.values.steering_input or 0
  p.speed = obj:getGroundSpeed()
  p.mask = mask
  p.hz = 1 / (stepsPerSend * (physicsDt or 0.0005))
  for i = 1, 4 do
    local wd = wheelSlots[i]
    local w = p.w[i - 1]
    if wd then
      w.down = wd.downForceRaw or 0
      w.slip = wd.lastSlip or 0
      w.sideSlip = wd.lastSideSlip or 0
      w.slipEnergy = wd.slipEnergy or 0
    else
      w.down, w.slip, w.sideSlip, w.slipEnergy = 0, 0, 0, 0
    end
  end
  rackSum, rackSteps = 0, 0
  local ok, sent = pcall(udp.send, udp, ffi.string(p, packetSize))
  if not ok or not sent then
    sendWait = stepsPerSend * BACKOFF -- Oversteer is not listening: look again now and then
  end
end

local function step(dtPhys)
  if not ready then setup() end
  local sum = 0
  for i = 1, rackCount do
    local h = rackHydros[i]
    if not h.fIsBroken(obj, h.bcid) then
      sum = sum + obj:getBeamStress(h.bcid) * h.inputFactor -- as hydros.lua:324
    end
  end
  rackSum = rackSum + sum
  rackSteps = rackSteps + 1
  stepCount = stepCount + 1
  if stepCount >= stepsPerSend then
    stepCount = 0
    if sendWait > 0 then
      sendWait = sendWait - stepsPerSend
      rackSum, rackSteps = 0, 0
    else
      send()
    end
  end
end

local failed = false

local function onPhysicsStep(dtPhys)
  if not udp or not playerInfo.firstPlayerSeated then return end
  local ok, err = pcall(step, dtPhys)
  if not ok then
    udp = nil -- quiet from here on
    if not failed then
      failed = true
      log("E", "countersteer_ffb", "stopped: " .. tostring(err))
    end
  end
end

local function enableHook()
  if hooked then return end
  hooked = true
  enablePhysicsStepHook()
end

local function onPlayersChanged()
  -- AI traffic and props never enable the hook: only the vehicle the player sits in
  if udp and playerInfo.firstPlayerSeated then enableHook() end
end

local function onReset()
  ready = false -- the wheels and hydros are looked at again on the next step
end

local function onExtensionLoaded()
  local ok, err = pcall(function()
    local cfg = jsonReadFile("/settings/countersteer_ffb.json")
    if type(cfg) == "table" then
      if type(cfg.port) == "number" and cfg.port >= 1024 and cfg.port <= 65535 then port = cfg.port end
      if type(cfg.hz) == "number" and cfg.hz >= 10 and cfg.hz <= 2000 then hz = cfg.hz end
    end
    packet = ffi.new("csb1_packet")
    packetSize = ffi.sizeof(packet)
    setupSocket()
    if udp and playerInfo.firstPlayerSeated then enableHook() end
  end)
  if not ok then
    udp = nil
    log("W", "countersteer_ffb", "not started: " .. tostring(err))
  else
    log("I", "countersteer_ffb", string.format("version %d, sending to %s:%d at up to %d Hz", MOD_VERSION, HOST, port, hz))
  end
end

M.onExtensionLoaded = onExtensionLoaded
M.onPhysicsStep = onPhysicsStep
M.onReset = onReset
M.onPlayersChanged = onPlayersChanged

return M
