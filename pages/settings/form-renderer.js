function pathJoin(prefix, key) {
  return prefix ? `${prefix}.${key}` : key;
}

function getCollapsedObjectPaths(options = {}) {
  if (options.collapsedObjectPaths instanceof Set) {
    return options.collapsedObjectPaths;
  }
  if (Array.isArray(options.collapsedObjectPaths)) {
    return new Set(options.collapsedObjectPaths);
  }
  return new Set();
}

function normalizeOptions(options = {}) {
  return {
    ...options,
    collapsedObjectPaths: getCollapsedObjectPaths(options),
  };
}

function isDisabledPath(path, options = {}) {
  if (typeof options.isFieldDisabled !== "function") {
    return false;
  }
  return Boolean(options.isFieldDisabled(path));
}

function setByPath(target, path, value) {
  const parts = path.split(".");
  let cursor = target;

  parts.forEach((part, index) => {
    if (index === parts.length - 1) {
      cursor[part] = value;
      return;
    }
    if (!cursor[part] || typeof cursor[part] !== "object") {
      cursor[part] = {};
    }
    cursor = cursor[part];
  });
}

function normalizeFileValue(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item) => typeof item === "string" && item.trim())
    .map((item) => item.trim());
}

function getFileBasename(filePath) {
  const normalized = String(filePath || "");
  const parts = normalized.split(/[\\/]/);
  return parts[parts.length - 1] || normalized;
}

function buildFileAccept(fileTypes) {
  if (!Array.isArray(fileTypes) || fileTypes.length === 0) {
    return "";
  }
  return fileTypes
    .map((extension) => String(extension || "").trim().replace(/^\.+/, ""))
    .filter(Boolean)
    .map((extension) => `.${extension}`)
    .join(",");
}

function showFileActionError(options, error, fallbackMessage) {
  const message = error?.message || fallbackMessage;
  if (typeof options.showToast === "function") {
    options.showToast(message, "error");
    return;
  }
  console.error(message, error);
}

function buildFileField(path, key, schema, value, options, disabled) {
  const field = document.createElement("div");
  field.className = "field file-field";
  if (disabled) {
    field.classList.add("is-disabled");
  }

  const copy = document.createElement("div");
  copy.className = "field-copy";

  const label = document.createElement("div");
  label.className = "field-label";
  label.textContent = schema.description || key;
  copy.appendChild(label);

  if (schema.hint) {
    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent = schema.hint;
    copy.appendChild(hint);
  }

  field.appendChild(copy);

  const control = document.createElement("div");
  control.className = "field-control";

  const list = document.createElement("div");
  list.className = "file-list";

  const uploadButton = document.createElement("button");
  uploadButton.type = "button";
  uploadButton.className = "upload-button file-upload-button";
  uploadButton.textContent = "上传文件";
  uploadButton.disabled = disabled;

  const hiddenInput = document.createElement("input");
  hiddenInput.type = "file";
  hiddenInput.className = "hidden-file-input";
  hiddenInput.dataset.path = path;
  hiddenInput.dataset.type = "file";
  hiddenInput.disabled = disabled;
  hiddenInput.tabIndex = -1;
  const accept = buildFileAccept(schema.file_types);
  if (accept) {
    hiddenInput.accept = accept;
  }

  let currentValue = normalizeFileValue(value);
  let busy = false;

  const renderList = () => {
    list.innerHTML = "";
    currentValue.forEach((filePath) => {
      const item = document.createElement("div");
      item.className = "file-item";

      const name = document.createElement("span");
      name.className = "file-name";
      name.textContent = getFileBasename(filePath);
      name.title = filePath;
      item.appendChild(name);

      const actions = document.createElement("div");
      actions.className = "file-actions";

      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "delete-button file-delete-button";
      deleteButton.textContent = "删除";
      deleteButton.disabled = disabled || busy;
      deleteButton.setAttribute("aria-label", `删除 ${getFileBasename(filePath)}`);
      deleteButton.addEventListener("click", async () => {
        if (disabled || busy) {
          return;
        }
        if (typeof options.onFileDelete !== "function") {
          showFileActionError(options, null, "删除功能不可用");
          return;
        }
        setBusy(true);
        try {
          const nextValue = await options.onFileDelete(path, filePath);
          syncValue(nextValue);
        } catch (error) {
          showFileActionError(options, error, "删除失败，请重试");
        } finally {
          setBusy(false);
        }
      });

      actions.appendChild(deleteButton);
      item.appendChild(actions);
      list.appendChild(item);
    });
  };

  const syncValue = (nextValue) => {
    currentValue = normalizeFileValue(nextValue);
    hiddenInput.dataset.value = JSON.stringify(currentValue);
    renderList();
  };

  const setBusy = (nextBusy) => {
    busy = Boolean(nextBusy);
    uploadButton.disabled = disabled || busy;
    hiddenInput.disabled = disabled || busy;
    uploadButton.textContent = busy ? "上传中..." : "上传文件";
    list.querySelectorAll("button").forEach((button) => {
      button.disabled = disabled || busy;
    });
  };

  hiddenInput.dataset.value = JSON.stringify(currentValue);
  renderList();

  uploadButton.addEventListener("click", () => {
    if (disabled || busy) {
      return;
    }
    if (typeof options.onFileUpload !== "function") {
      showFileActionError(options, null, "上传功能不可用");
      return;
    }
    hiddenInput.click();
  });

  hiddenInput.addEventListener("change", async () => {
    const file = hiddenInput.files?.[0];
    hiddenInput.value = "";
    if (!file || disabled || busy) {
      return;
    }
    if (typeof options.onFileUpload !== "function") {
      showFileActionError(options, null, "上传功能不可用");
      return;
    }
    setBusy(true);
    try {
      const nextValue = await options.onFileUpload(path, file);
      syncValue(nextValue);
    } catch (error) {
      showFileActionError(options, error, "上传失败，请重试");
    } finally {
      setBusy(false);
    }
  });

  control.appendChild(list);
  control.appendChild(uploadButton);
  control.appendChild(hiddenInput);
  field.appendChild(control);
  return field;
}

function buildField(path, key, schema, value, options = {}) {
  const type = schema.type || "string";
  const disabled = isDisabledPath(path, options);

  if (type === "object") {
    const wrapper = document.createElement("section");
    wrapper.className = "form-object";
    const isCollapsible = options.collapsedObjectPaths.has(path);
    if (disabled) {
      wrapper.classList.add("is-disabled");
    }
    let bodyHost = wrapper;

    if (isCollapsible) {
      wrapper.classList.add("is-collapsible");

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "form-object-toggle";
      toggle.disabled = disabled;

      const copy = document.createElement("span");
      copy.className = "form-object-toggle-copy section-head";

      const title = document.createElement("span");
      title.className = "section-title";
      title.textContent = schema.description || key;
      copy.appendChild(title);

      if (schema.hint) {
        const hint = document.createElement("span");
        hint.className = "section-hint";
        hint.textContent = schema.hint;
        copy.appendChild(hint);
      }

      const action = document.createElement("span");
      action.className = "form-object-toggle-action";

      let collapsed = true;
      const syncCollapsedState = () => {
        wrapper.classList.toggle("is-collapsed", collapsed);
        toggle.setAttribute("aria-expanded", String(!collapsed));
        action.textContent = collapsed ? "展开" : "收起";
      };

      toggle.appendChild(copy);
      toggle.appendChild(action);
      toggle.addEventListener("click", () => {
        collapsed = !collapsed;
        syncCollapsedState();
      });
      wrapper.appendChild(toggle);

      const body = document.createElement("div");
      body.className = "form-object-body";
      wrapper.appendChild(body);
      bodyHost = body;

      syncCollapsedState();
    } else {
      const header = document.createElement("div");
      header.className = "section-head";

      const title = document.createElement("div");
      title.className = "section-title";
      title.textContent = schema.description || key;
      header.appendChild(title);

      if (schema.hint) {
        const hint = document.createElement("div");
        hint.className = "section-hint";
        hint.textContent = schema.hint;
        header.appendChild(hint);
      }

      wrapper.appendChild(header);
    }

    const grid = document.createElement("div");
    grid.className = "field-grid";
    if (options.singleColumn) {
      grid.classList.add("single-column");
    }
    Object.entries(schema.items || {}).forEach(([childKey, childSchema]) => {
      grid.appendChild(
        buildField(
          pathJoin(path, childKey),
          childKey,
          childSchema,
          value?.[childKey] ?? childSchema.default,
          options
        )
      );
    });
    bodyHost.appendChild(grid);
    return wrapper;
  }

  if (type === "file") {
    return buildFileField(path, key, schema, value, options, disabled);
  }

  const field = document.createElement("label");
  field.className = "field";
  if (type === "bool") {
    field.classList.add("checkbox-field");
  }
  if (disabled) {
    field.classList.add("is-disabled");
  }

  const copy = document.createElement("div");
  copy.className = "field-copy";

  const label = document.createElement("div");
  label.className = "field-label";
  label.textContent = schema.description || key;
  copy.appendChild(label);

  if (schema.hint) {
    const hint = document.createElement("div");
    hint.className = "field-hint";
    hint.textContent = schema.hint;
    copy.appendChild(hint);
  }

  field.appendChild(copy);

  const control = document.createElement("div");
  control.className = "field-control";

  let input;
  if (type === "bool") {
    const shell = document.createElement("span");
    shell.className = "switch";
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
    const slider = document.createElement("span");
    slider.className = "slider";
    shell.appendChild(input);
    shell.appendChild(slider);
    control.appendChild(shell);
  } else if (schema.options?.length) {
    input = document.createElement("select");
    schema.options.forEach((option) => {
      const node = document.createElement("option");
      node.value = option;
      node.textContent = option;
      if (String(value ?? schema.default ?? "") === option) {
        node.selected = true;
      }
      input.appendChild(node);
    });
    control.appendChild(input);
  } else if (type === "int") {
    input = document.createElement("input");
    input.type = "number";
    input.value = String(value ?? schema.default ?? 0);
    if (schema.slider) {
      if (schema.slider.min !== undefined) input.min = schema.slider.min;
      if (schema.slider.max !== undefined) input.max = schema.slider.max;
      if (schema.slider.step !== undefined) input.step = schema.slider.step;
    }
    control.appendChild(input);
  } else if (type === "list") {
    input = document.createElement("textarea");
    input.value = Array.isArray(value) ? value.join("\n") : "";
    input.placeholder = "每行一个条目，也支持粘贴后分行整理";
    control.appendChild(input);
  } else {
    const multiline =
      type === "text" ||
      String(value || "").includes("\n") ||
      key.includes("welcome");
    input = document.createElement(multiline ? "textarea" : "input");
    if (!multiline) {
      input.type = "text";
    }
    input.value = String(value ?? schema.default ?? "");
    control.appendChild(input);
  }

  input.dataset.path = path;
  input.dataset.type = type;
  input.disabled = disabled;
  field.appendChild(control);
  return field;
}

export function renderSchemaFields(root, schema, values, options = {}) {
  const normalizedOptions = normalizeOptions(options);
  root.innerHTML = "";

  const grid = document.createElement("div");
  grid.className = "field-grid";
  if (normalizedOptions.singleColumn) {
    grid.classList.add("single-column");
  }

  Object.entries(schema).forEach(([key, fieldSchema]) => {
    grid.appendChild(
      buildField(
        key,
        key,
        fieldSchema,
        values?.[key] ?? fieldSchema.default,
        normalizedOptions
      )
    );
  });

  root.appendChild(grid);
}

export function collectFormData(root) {
  const payload = {};
  root.querySelectorAll("[data-path]").forEach((node) => {
    const { path, type } = node.dataset;
    let value;

    if (type === "file") {
      try {
        value = normalizeFileValue(JSON.parse(node.dataset.value || "[]"));
      } catch {
        value = [];
      }
    } else if (type === "bool") {
      value = node.checked;
    } else if (type === "int") {
      value = Number(node.value || 0);
    } else if (type === "list") {
      value = node.value
        .split(/\n+/)
        .map((item) => item.trim())
        .filter(Boolean);
    } else {
      value = node.value;
    }

    setByPath(payload, path, value);
  });
  return payload;
}
