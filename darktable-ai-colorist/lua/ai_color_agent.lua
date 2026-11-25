local dt = require "darktable"
local script = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local entrypoint = assert(script:match("^(.*)/lua/[^/]+$"), "Load ai_color_agent.lua from its project lua/ directory")
  .. "/python/dt_ai_agent.py"
local darkroom, lighttable = dt.gui.views.darkroom, dt.gui.views.lighttable
local windows = dt.configuration.running_os == "windows"
local plugin = "ai_color_agent"

dt.preferences.register(plugin, "python", "string", "AI Colorist Python",
  "Python 3.10+ executable with the project's requirements installed", windows and "python" or "python3")
dt.preferences.register(plugin, "steps", "integer", "AI Colorist maximum iterations",
  "Maximum number of model observations per turn", 3, 1, 20)

local function hex(value)
  return (tostring(value):gsub(".", function(byte) return string.format("%02x", byte:byte()) end))
end

local function unhex(value)
  return (value:gsub("..", function(pair) return string.char(tonumber(pair, 16)) end))
end

local function write(path, value)
  local file = assert(io.open(path, "wb"))
  assert(file:write(value))
  assert(file:close())
end

local function read(path)
  local file = io.open(path, "rb")
  if not file then return nil end
  local value = file:read("*a")
  file:close()
  return value
end

local function shell_quote(value)
  return "'" .. value:gsub("'", "'\\''") .. "'"
end

local function base64(value)
  local alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
  local function digit(number)
    return alphabet:sub(number + 1, number + 1)
  end
  local encoded = {}
  for offset = 1, #value, 3 do
    local a, b, c = value:byte(offset, offset + 2)
    local group = (a << 16) | ((b or 0) << 8) | (c or 0)
    encoded[#encoded + 1] = digit(group >> 18) .. digit((group >> 12) & 63)
      .. (b and digit((group >> 6) & 63) or "=") .. (c and digit(group & 63) or "=")
  end
  return table.concat(encoded)
end

local function command(request)
  local python = dt.preferences.read(plugin, "python", "string")
  if windows then
    local function ps(value) return "'" .. value:gsub("'", "''") .. "'" end
    local expression = "& " .. ps(python) .. " " .. ps(entrypoint) .. " '--ui-request' "
      .. ps(request) .. "; exit $LASTEXITCODE"
    local utf16 = {}
    for _, code in utf8.codes(expression) do
      if code < 0x10000 then
        utf16[#utf16 + 1] = string.pack("<I2", code)
      else
        code = code - 0x10000
        utf16[#utf16 + 1] = string.pack("<I2I2", 0xd800 | (code >> 10), 0xdc00 | (code & 0x3ff))
      end
    end
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " .. base64(table.concat(utf16))
  end
  return shell_quote(python) .. " " .. shell_quote(entrypoint) .. " --ui-request " .. shell_quote(request)
end

local prefix = dt.configuration.tmp_dir .. "/dt-colorist-" .. os.time() .. "-"
  .. tostring({}):gsub("[^%w]", "") .. "-"
local request_number = 0

local function invoke(arguments)
  request_number = request_number + 1
  local filename = prefix .. request_number
  local rows = {}
  for name, value in pairs(arguments) do
    rows[#rows + 1] = name .. "=" .. hex(value) .. "\n"
  end
  local ok, result = pcall(function()
    write(filename .. ".request", table.concat(rows))
    local status = dt.control.execute(command(filename .. ".request"))
    local contents = read(filename .. ".reply")
    if not contents then
      error("Python returned no response (exit " .. tostring(status) .. "). Check the Python executable and requirements.")
    end
    local reply = {}
    for key, value in contents:gmatch("([%w_]+)=([%da-f]*)\r?\n") do reply[key] = unhex(value) end
    if reply.ok ~= "True" then error(reply.message or "Python returned an invalid response.") end
    return reply
  end)
  os.remove(filename .. ".request")
  os.remove(filename .. ".reply")
  if not ok then error(result, 0) end
  return result
end

local model = dt.new_widget("combobox"){label = "Model", "antigravity/default", selected = 1}
local steps = dt.new_widget("combobox"){label = "Max iterations"}
for index = 1, 20 do steps[index] = tostring(index) end
steps.selected = dt.preferences.read(plugin, "steps", "integer")
steps.changed_callback = function(widget)
  if widget.selected > 0 then dt.preferences.write(plugin, "steps", "integer", widget.selected) end
end
local composer = dt.new_widget("entry"){placeholder = "Describe the look you want"}
local transcript = dt.new_widget("text_view"){editable = false, text = ""}
local loading = dt.new_widget("label"){halign = "start", label = ""}
local chats, active_chat, busy = {}, nil, false
local send, toggle, approve, refresh

local function current_image()
  if dt.gui.current_view().id == "darkroom" then
    return darkroom.display_image()
  end
end

local function buttons()
  local review = active_chat and active_chat.review
  send.sensitive = not busy and active_chat ~= nil
  composer.sensitive = send.sensitive
  composer.placeholder = review and "Tell AI what to adjust" or "Describe the look you want"
  refresh.sensitive = not busy
  model.sensitive, steps.sensitive = not busy, not busy
  approve.sensitive = not busy and review ~= nil
  toggle.sensitive = not busy and review ~= nil
  toggle.label = review and (review.pending or review.showing_before) and "Show After" or "Show Before"
end

local function show_chat()
  local rows = {}
  if active_chat then
    -- Keep the newest exchange closest to the composer in Darkroom's scrollable panel.
    for index = #active_chat.turns, 1, -1 do
      local turn = active_chat.turns[index]
      local row = "You\n" .. turn.message
      if turn.reply then row = row .. "\n\nAI Colorist\n" .. turn.reply end
      if turn.notice then row = row .. "\n\n" .. turn.notice end
      rows[#rows + 1] = row
    end
  end
  transcript.text = table.concat(rows, "\n\n")
  transcript.visible = #rows > 0
end

local function activate_chat(image)
  local chat = chats[image.id]
  if not chat then
    chat = {turns = {}, draft = ""}
    chats[image.id] = chat
  end
  if active_chat ~= chat then
    if active_chat then active_chat.draft = composer.text end
    active_chat = chat
    composer.text = chat.draft
    show_chat()
    buttons()
  end
  return chat
end

local function during_job(callback, status, on_error)
  if busy then return end
  busy = true
  loading.label = status or ""
  loading.visible = status ~= nil
  buttons()
  local job
  local ok, message = pcall(function()
    if status then job = dt.gui.create_job(status, false) end
    callback()
  end)
  if job then job.valid = false end
  busy = false
  loading.visible = false
  if not ok then
    message = tostring(message):gsub("^.-:%d+: ", "")
    if on_error then on_error(message) end
    dt.print(message)
  end
  buttons()
end

local function chat_image()
  local image = current_image()
  if not image or image.id <= 0 then error("Open an image in Darkroom first.") end
  return image, activate_chat(image)
end

local function in_lighttable(image, callback)
  dt.gui.current_view(lighttable)
  for _ = 1, 50 do
    if dt.gui.current_view().id == "lighttable" then break end
    dt.control.sleep(50)
  end
  if dt.gui.current_view().id ~= "lighttable" then error("Could not save the Darkroom history.") end
  local ok, result = pcall(callback)
  dt.gui.current_view(darkroom)
  for _ = 1, 100 do
    local active = current_image()
    if active and active.id == image.id then break end
    if active then darkroom.display_image(image) end
    dt.control.sleep(50)
  end
  local active = current_image()
  if not active or active.id ~= image.id then error("Return to the original Darkroom image and retry.") end
  if not ok then error(result, 0) end
  return result
end

local function save_darkroom(image)
  -- Leaving Darkroom flushes the current history and metadata to the native sidecar.
  local snapshot = in_lighttable(image, function() return read(image.sidecar) end)
  if not snapshot then error("Enable XMP sidecar writing on import, reopen this image, and try again.") end
  return snapshot
end

local function display_sidecar(image, path)
  -- Darktable 5.6.2 can block while reloading history for the active Darkroom image.
  in_lighttable(image, function()
    if not image:apply_sidecar(path) then error("Darktable could not apply the review XMP.") end
  end)
end

local function forget_review(chat)
  local review = assert(chat.review, "There is no grade to approve.")
  invoke{mode = "approve", session_id = review.session_id}
  os.remove(review.before)
  os.remove(review.after)
  chat.review = nil
end

local function ask(mode, image, state, source, output, message)
  local arguments = {
    mode = mode, session_id = state.session_id, raw = image.path .. "/" .. image.filename,
    xmp = source, output_xmp = output, model = model.value or "antigravity/default",
    max_steps = steps.selected,
    is_raw = image.is_raw, maker = image.exif_maker or "", camera = image.exif_model or "",
    darktable_config = dt.configuration.config_dir,
  }
  arguments[mode == "run" and "prompt" or "feedback"] = message
  if image.is_raw then
    -- The public action uses native slider units when reading with effect "set".
    local temperature = dt.gui.action("iop/temperature/temperature", "value", "set")
    local tint = dt.gui.action("iop/temperature/tint", "value", "set")
    if temperature >= 1901 and temperature <= 25000 and tint >= 0.135 and tint <= 2.326 then
      arguments.temperature = temperature
      arguments.tint = tint
    end
  end
  return invoke(arguments)
end

local function review_turn(image, chat, turn)
  local state = chat.review
  local is_refine = state ~= nil
  if state and (state.showing_before or state.pending) then
    display_sidecar(image, state.after)
    state.showing_before, state.pending = false, false
  end
  if not state then
    local id = "dt-" .. image.id .. "-" .. os.time() .. "-" .. tostring({}):gsub("[^%w]", "")
    state = {session_id = id, before = prefix .. id .. ".before.xmp",
      after = prefix .. id .. ".after.xmp", showing_before = false}
  end
  local source = is_refine and (state.after .. ".input.xmp") or state.before
  local output = state.after .. ".candidate.xmp"
  write(source, save_darkroom(image))
  local changed_at = image.change_timestamp
  local ok, reply = pcall(ask, is_refine and "refine" or "run", image, state, source, output, turn.message)
  if is_refine then os.remove(source) end
  if not ok then
    os.remove(output)
    if not is_refine then os.remove(state.before) end
    error(reply, 0)
  end
  local data = assert(read(output), "The session did not return a review XMP.")
  write(state.after, data)
  os.remove(output)
  state.pending = reply.changed == "True"
  chat.review = state
  turn.reply = reply.message
  local active = current_image()
  if state.pending then
    if active and active.id == image.id and active.change_timestamp == changed_at then
      local applied = pcall(display_sidecar, active, state.after)
      state.pending = not applied
    end
    if state.pending then turn.notice = "Preview ready. Use Show After to apply it." end
  end
end

refresh = dt.new_widget("button"){label = "Refresh Models", clicked_callback = function()
  during_job(function()
    local reply = invoke{mode = "list-models"}
    local previous = model.value
    for index = #model, 1, -1 do model[index] = nil end
    local selected, index = 1, 0
    for item in reply.models:gmatch("[^\n]+") do
      index = index + 1
      model[index] = item
      if item == previous then selected = index end
    end
    model.selected = selected
    if reply.warnings and reply.warnings ~= "" then dt.print(reply.warnings) end
  end)
end}
send = dt.new_widget("button"){label = "Send", clicked_callback = function()
  local chat, turn
  during_job(function()
    local image
    image, chat = chat_image()
    local message = composer.text
    if not message:match("%S") then error("Enter a message first.") end
    turn = {message = message}
    chat.turns[#chat.turns + 1] = turn
    composer.text, chat.draft = "", ""
    show_chat()
    review_turn(image, chat, turn)
    if active_chat == chat then show_chat() end
  end, "AI is processing…", function(message)
    if turn then
      turn.notice = "Could not send\n" .. message
      chat.draft = turn.message
      if active_chat == chat then
        composer.text = chat.draft
        show_chat()
      end
    end
  end)
end}
toggle = dt.new_widget("button"){label = "Show Before", clicked_callback = function()
  during_job(function()
    local image, chat = chat_image()
    local review = assert(chat.review, "There is no preview to compare.")
    if review.pending or review.showing_before then
      display_sidecar(image, review.after)
      review.pending, review.showing_before = false, false
    else
      display_sidecar(image, review.before)
      review.showing_before = true
    end
  end)
end}
approve = dt.new_widget("button"){label = "Approve", clicked_callback = function()
  during_job(function()
    local image, chat = chat_image()
    local review = assert(chat.review, "There is no grade to approve.")
    if review.pending or review.showing_before then display_sidecar(image, review.after) end
    save_darkroom(image)
    forget_review(chat)
  end)
end}

buttons()
dt.register_lib(plugin, "AI Colorist", true, false,
  {[darkroom] = {"DT_UI_CONTAINER_PANEL_RIGHT_CENTER", 999}},
  dt.new_widget("box"){
    orientation = "vertical",
    model, refresh, steps, composer, send, loading, toggle, approve, transcript,
  })
loading.visible, transcript.visible = false, false
dt.register_event(plugin, "darkroom-image-loaded", function(_, _, image)
  if image and image.id > 0 and dt.gui.current_view().id == "darkroom" then activate_chat(image) end
end)
local image = current_image()
if image and image.id > 0 then activate_chat(image) end
