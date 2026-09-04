import ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
APP=(ROOT/'app.py').read_text(encoding='utf-8')
HTML=(ROOT/'components'/'product_transform'/'index.html').read_text(encoding='utf-8')


def test_apply_normalizes_before_manual_override_commit():
    start=APP.index('def apply_editor_tuning_and_sync(')
    end=APP.index('def clear_editor_widget_state_for_product', start)
    chunk=APP[start:end]
    assert chunk.index('render_editor_normalized_result(') < chunk.index('"manual_overrides"')
    assert '_snapshot_product_apply_state' in chunk
    assert '_restore_product_apply_state' in chunk


def test_failed_scan_does_not_pop_previous_detection_state():
    pos=APP.index('Detection failed:')
    chunk=APP[pos-300:pos+500]
    assert 'pop(\n                "detection_result"' not in chunk
    assert 'previous successful Detection result was preserved' in chunk


def test_failed_advanced_audit_does_not_pop_previous_state():
    pos=APP.index('Advanced audit failed:')
    chunk=APP[pos-300:pos+500]
    assert 'pop(\n                "advanced_audit_pages"' not in chunk
    assert 'previous successful Advanced Audit was preserved' in chunk


def test_reset_token_uses_geometry_helper():
    assert 'reset_token = geometry_reset_token(' in APP


def test_browser_crop_limit_matches_python_45_percent():
    assert HTML.count('max="45"') == 4
    assert 'clamp(Number(element.value) / 100, 0, .45)' in HTML
    assert 'max="40"' not in HTML



def test_filtered_product_include_uses_full_app_rerun():
    """Regression for StreamlitInvalidLayoutContextError from filtered inspector."""
    start=APP.index('def render_filtered_product_inspector(')
    end=APP.index('def filtered_items_csv_with_page_bytes(', start)
    chunk=APP[start:end]
    assert 'scope="fragment"' not in chunk
    assert 'Including a filtered' in chunk
    assert 'st.rerun()' in chunk


def test_fragment_scoped_reruns_are_lexically_inside_fragment_functions():
    """Do not put scope='fragment' reruns in ordinary top-level/helper functions."""
    tree=ast.parse(APP)
    violations=[]

    def is_fragment_decorator(decorator):
        return (
            isinstance(decorator, ast.Attribute)
            and isinstance(decorator.value, ast.Name)
            and decorator.value.id == 'st'
            and decorator.attr == 'fragment'
        )

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.fragment_stack=[]

        def visit_FunctionDef(self, node):
            is_fragment=any(is_fragment_decorator(d) for d in node.decorator_list)
            self.fragment_stack.append(is_fragment)
            self.generic_visit(node)
            self.fragment_stack.pop()

        visit_AsyncFunctionDef=visit_FunctionDef

        def visit_Call(self, node):
            is_st_rerun=(
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'st'
                and node.func.attr == 'rerun'
            )
            if is_st_rerun:
                fragment_scope=any(
                    kw.arg == 'scope'
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == 'fragment'
                    for kw in node.keywords
                )
                if fragment_scope and not any(self.fragment_stack):
                    violations.append(node.lineno)
            self.generic_visit(node)

    Visitor().visit(tree)
    assert violations == [], f'Illegal fragment-scoped reruns at lines: {violations}'
