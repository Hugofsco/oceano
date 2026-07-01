"""The agent's tools. Each tool = a JSON schema (shown to the model) + a Python
function (run by us). Add a tool by writing a function and decorating it.

File/shell ops default to the WORKSPACE folder so the agent has a real place to
work, without roaming the whole disk. Set OCEANO_CONFINE=0 to lift the fence.
"""

# Compatibility facade: `from oceano import tools` keeps working exactly as before the
# split — the infrastructure lives in oceano.tools.core, the tools in domain modules.
from oceano.tools.core import (  # noqa: F401
    MEMORY_TOOLS,
    _SCHEMAS,
    _STATE_PATH,
    _TOOLS,
    _load_state,
    _resolve,
    _save_state,
    _ws,
    all_schemas,
    background,
    background_workspace,
    channel,
    chat_tool_state,
    chat_tools,
    clear_progress_sink,
    current_channel,
    emit_progress,
    is_background,
    is_enabled,
    live_browser_available,
    register,
    run,
    schemas,
    set_all,
    set_chat_tool,
    set_enabled,
    set_progress_sink,
    tool,
    unregister_prefix,
)

# Import every domain module so their @tool decorators register. The order mirrors the
# original single-file layout so the schema listing (Settings -> Tools) stays stable.
from oceano.tools import (  # noqa: E402, F401
    files,
    shell,
    web,
    knowledge,
    browsing,
    sched,
    hosts_tools,
    mail_tools,
    selfimprove,
    calendar_tools,
    media,
    dev,
    ui,
)

# Re-export the tool functions (and the private gates/helpers tests rely on) so
# `tools.X` keeps resolving for every existing caller.
from oceano.tools.files import (  # noqa: E402, F401
    edit_file,
    list_files,
    make_folder,
    read_file,
    write_file,
)
from oceano.tools.shell import (  # noqa: E402, F401
    _SHELL_TAINTED,
    _bwrap_base,
    _sandbox_ok,
    _sandbox_wrap,
    _shell_blocked,
    job_status,
    python_exec,
    run_shell,
    spawn_job,
)
from oceano.tools.web import (  # noqa: E402, F401
    _HTTP_HEADERS,
    _http_fetch,
    fetch_url,
    http_request,
    rss,
    web_search,
)
from oceano.tools.knowledge import (  # noqa: E402, F401
    forget_memory,
    index_docs,
    list_skills,
    load_skill,
    recall,
    remember,
    search_chats,
    search_docs,
    update_memory,
)
from oceano.tools.browsing import (  # noqa: E402, F401
    browser_click,
    browser_dialog,
    browser_eval,
    browser_extract,
    browser_fill,
    browser_hover,
    browser_open,
    browser_press,
    browser_read,
    browser_screenshot,
    browser_scroll,
    browser_select,
    browser_snapshot,
    browser_tab,
    browser_upload,
    browser_wait,
)
from oceano.tools.sched import (  # noqa: E402, F401
    _run_one_workflow,
    accept_suggestion,
    cancel_task,
    dismiss_suggestion,
    list_suggestions,
    list_tasks,
    list_workflows,
    notify,
    run_workflow,
    schedule_task,
    update_task,
)
from oceano.tools.hosts_tools import (  # noqa: E402, F401
    list_hosts,
    sftp,
    ssh_run,
)
from oceano.tools.mail_tools import (  # noqa: E402, F401
    _MAIL_SEND_TAINTED,
    _MAIL_WEB_ONLY,
    _mail_target,
    mail_accounts,
    mail_delete,
    mail_flag,
    mail_folder,
    mail_folders,
    mail_list,
    mail_move,
    mail_read,
    mail_reply,
    mail_save_attachment,
    mail_send,
)
from oceano.tools.selfimprove import (  # noqa: E402, F401
    agent_status,
    delegate_tool,
    evaluate_skill,
    learn_skill,
    spawn_agent,
)
from oceano.tools.calendar_tools import (  # noqa: E402, F401
    _format_ops,
    add_calendar_event,
    add_calendar_events,
    calendar_events,
    delete_calendar_event,
    find_free_slots,
    manage_calendar,
    update_calendar_event,
)
from oceano.tools.media import (  # noqa: E402, F401
    convert,
    fetch_media,
    speak_to_file,
    transcribe_media,
)
from oceano.tools.dev import (  # noqa: E402, F401
    _GIT_OK,
    code_search,
    git,
    run_tests,
    sql_query,
)
from oceano.tools.ui import (  # noqa: E402, F401
    ui_arrange,
    ui_close,
    ui_open,
)
