import ast

from dbt.parser.languages.provider import LanguageProvider, dbt_function_calls
from dbt.exceptions import (
    UndefinedMacroError,
    ParsingError,
    PythonLiteralEvalError,
    PythonParsingError,
)
from dbt.contracts.graph.nodes import ManifestNode

from typing import Dict, Any

dbt_function_key_words = set(["ref", "source", "config", "get"])
dbt_function_full_names = set(["dbt.ref", "dbt.source", "dbt.config", "dbt.config.get"])


class PythonValidationVisitor(ast.NodeVisitor):
    def __init__(self):
        super().__init__()
        self.dbt_errors = []
        self.num_model_def = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == "model":
            self.num_model_def += 1
            if node.args.args and not node.args.args[0].arg == "dbt":
                self.dbt_errors.append("'dbt' not provided for model as the first argument")
            if len(node.args.args) != 2:
                self.dbt_errors.append(
                    "model function should have two args, `dbt` and a session to current warehouse"
                )
            # check we have a return and only one
            if not isinstance(node.body[-1], ast.Return) or isinstance(
                node.body[-1].value, ast.Tuple
            ):
                self.dbt_errors.append(
                    "In current version, model function should return only one dataframe object"
                )

    def check_error(self, node):
        if self.num_model_def != 1:
            raise ParsingError(
                f"dbt allows exactly one model defined per python file, found {self.num_model_def}",
                node=node,
            )

        if len(self.dbt_errors) != 0:
            raise ParsingError("\n".join(self.dbt_errors), node=node)


class PythonParseVisitor(ast.NodeVisitor):
    def __init__(self, dbt_node):
        super().__init__()

        self.dbt_node = dbt_node
        self.dbt_function_calls = []
        self.packages = []

    @classmethod
    def _flatten_attr(cls, node):
        if isinstance(node, ast.Attribute):
            return str(cls._flatten_attr(node.value)) + "." + node.attr
        elif isinstance(node, ast.Name):
            return str(node.id)
        else:
            pass

    def _safe_eval(self, node):
        try:
            return ast.literal_eval(node)
        except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError) as exc:
            raise PythonLiteralEvalError(exc, node=self.dbt_node) from exc

    def _get_call_literals(self, node):
        # List of literals
        arg_literals = []
        kwarg_literals = {}

        # TODO : Make sure this throws (and that we catch it)
        # for non-literal inputs
        for arg in node.args:
            rendered = self._safe_eval(arg)
            arg_literals.append(rendered)

        for keyword in node.keywords:
            key = keyword.arg
            rendered = self._safe_eval(keyword.value)
            kwarg_literals[key] = rendered

        return arg_literals, kwarg_literals

    def visit_Call(self, node: ast.Call) -> None:
        # check weather the current call could be a dbt function call
        if isinstance(node.func, ast.Attribute) and node.func.attr in dbt_function_key_words:
            func_name = self._flatten_attr(node.func)
            # check weather the current call really is a dbt function call
            if func_name in dbt_function_full_names:
                # drop the dot-dbt prefix
                func_name = func_name.split(".")[-1]
                args, kwargs = self._get_call_literals(node)
                self.dbt_function_calls.append((func_name, args, kwargs))

        # no matter what happened above, we should keep visiting the rest of the tree
        # visit args and kwargs to see if there's call in it
        for obj in node.args + [kwarg.value for kwarg in node.keywords]:
            if isinstance(obj, ast.Call):
                self.visit_Call(obj)
            # support dbt.ref in list args, kwargs
            elif isinstance(obj, ast.List) or isinstance(obj, ast.Tuple):
                for el in obj.elts:
                    if isinstance(el, ast.Call):
                        self.visit_Call(el)
            # support dbt.ref in dict args, kwargs
            elif isinstance(obj, ast.Dict):
                for value in obj.values:
                    if isinstance(value, ast.Call):
                        self.visit_Call(value)
        # visit node.func.value if we are at an call attr
        if isinstance(node.func, ast.Attribute):
            self.attribute_helper(node.func)

    def attribute_helper(self, node: ast.Attribute) -> None:
        while isinstance(node, ast.Attribute):
            node = node.value  # type: ignore
        if isinstance(node, ast.Call):
            self.visit_Call(node)

    def visit_Import(self, node: ast.Import) -> None:
        for n in node.names:
            self.packages.append(n.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.packages.append(node.module.split(".")[0])


def verify_python_model_code(node):
    from dbt.clients.jinja import get_rendered

    # TODO: add a test for this
    try:
        rendered_python = get_rendered(
            node.raw_code,
            {},
            node,
        )
        if rendered_python != node.raw_code:
            raise ParsingError("")
    except (UndefinedMacroError, ParsingError):
        raise ParsingError("No jinja in python model code is allowed", node=node)


class PythonProvider(LanguageProvider):
    @classmethod
    def name(self) -> str:
        return "python"

    @classmethod
    def file_ext(self) -> str:
        return ".py"

    @classmethod
    def extract_dbt_function_calls(self, node) -> dbt_function_calls:
        """
        List all references (refs, sources, configs) in a given block.
        """
        try:
            tree = ast.parse(node.raw_code, filename=node.original_file_path)
        except SyntaxError as exc:
            raise PythonParsingError(exc, node=node) from exc

        # Only parse if AST tree has instructions in body
        if tree.body:
            # We are doing a validator and a parser because visit_FunctionDef in parser
            # would actually make the parser not doing the visit_Calls any more
            dbt_validator = PythonValidationVisitor()
            dbt_validator.visit(tree)
            dbt_validator.check_error(node)

            dbt_parser = PythonParseVisitor(node)
            dbt_parser.visit(tree)
            return dbt_parser.dbt_function_calls
        else:
            return []

    @classmethod
    def validate_raw_code(self, node) -> None:
        from dbt.clients.jinja import get_rendered

        # TODO: add a test for this
        try:
            rendered_python = get_rendered(
                node.raw_code,
                {},
                node,
            )
            if rendered_python != node.raw_code:
                raise ParsingError("")
        except (UndefinedMacroError, ParsingError):
            raise ParsingError("No jinja in python model code is allowed", node=node)

    @classmethod
    def get_compiled_code(self, node: ManifestNode, context: Dict[str, Any]) -> str:
        # needed for compilation - bad!!
        from dbt.clients import jinja

        postfix = jinja.get_rendered(
            "{{ py_script_postfix(model) }}",
            context,
            node,
        )
        # we should NOT jinja render the python model's 'raw code'
        return f"{node.raw_code}\n\n{postfix}"

    @classmethod
    def needs_compile_time_connection(self) -> bool:
        return False
