import json

from fastapi import HTTPException, Query, APIRouter
from sqlalchemy import cast, column, exists
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.operators import ColumnOperators
from starlette.requests import Request
from starlette.status import HTTP_404_NOT_FOUND

from parsimonious.grammar import Grammar
from parsimonious.nodes import NodeVisitor

from .models import db, Metadata

mod = APIRouter()


@mod.get("/metadata")
async def search_metadata(
    request: Request,
    data: bool = Query(
        False,
        description="Switch to returning a list of GUIDs (false), "
        "or GUIDs mapping to their metadata (true).",
    ),
    limit: int = Query(
        10, description="Maximum number of records returned. (max: 2000)"
    ),
    offset: int = Query(0, description="Return results at this given offset."),
):
    """Search the metadata.

    Without filters, this will return all data. Add filters as query strings like this:

        GET /metadata?a=1&b=2

    This will match all records that have metadata containing all of:

        {"a": 1, "b": 2}

    The values are always treated as strings for filtering. Nesting is supported:

        GET /metadata?a.b.c=3

    Matching records containing:

        {"a": {"b": {"c": 3}}}

    Providing the same key with more than one value filters records whose value of the
    given key matches any of the given values. But values of different keys must all
    match. For example:

        GET /metadata?a.b.c=3&a.b.c=33&a.b.d=4

    Matches these:

        {"a": {"b": {"c": 3, "d": 4}}}
        {"a": {"b": {"c": 33, "d": 4}}}
        {"a": {"b": {"c": "3", "d": 4, "e": 5}}}

    But won't match these:

        {"a": {"b": {"c": 3}}}
        {"a": {"b": {"c": 3, "d": 5}}}
        {"a": {"b": {"d": 5}}}
        {"a": {"b": {"c": "333", "d": 4}}}

    """
    limit = min(limit, 2000)
    queries = {}
    for key, value in request.query_params.multi_items():
        if key not in {"data", "limit", "offset"}:
            queries.setdefault(key, []).append(value)

    def add_filters():
        filter_param = ""

        if queries:
            filter_param = "(and"
            for path, values in queries.items():
                filter_param += ",(or"
                for v in values:
                    #  to maintain backwards compatibility for this endpoint,
                    #  use :like instead of :eq because SQL LIKE is a textual
                    #  operator. XXX will need to escape %
                    filter_param += f',({path},:like,"{v}")'
                filter_param += ")"
            filter_param += ")"

        return filter_param

    return await search_metadata_helper(
        data=data,
        limit=limit,
        offset=offset,
        filter=add_filters(),
    )


async def search_metadata_helper(
    data: bool = Query(
        False,
        description="Switch to returning a list of GUIDs (false), "
        "or GUIDs mapping to their metadata (true).",
    ),
    limit: int = Query(
        10, description="Maximum number of records returned. (max: 2000)"
    ),
    offset: int = Query(0, description="Return results at this given offset."),
    #  XXX description
    #  XXX how to name this python variable something other than filter but
    #  still have client use "filter" as URL query param?
    filter: str = Query("", description="Filters to apply."),
):
    """
    XXX comments
    """
    limit = min(limit, 2000)

    def add_filter(target_key, operator_name, other):
        json_object = Metadata.data[list(target_key.split("."))]

        if type(other) is dict:
            #  since other isn't a primitive type, it means that this is a compound
            #  operator
            #  (e.g. GET objects?filter=(_resource_paths,:any,(,:like,"/programs/%")) )
            return operators[operator_name]["sql_clause"](
                json_object, other["op"], other["val"]
            )

        if (
            "type" in operators[operator_name]
            and operators[operator_name]["type"] == "text"
        ):
            json_object = json_object.astext
        else:
            other = cast(other, JSONB)

        return operators[operator_name]["sql_comparator"](json_object, other)

    #  XXX get_any_sql_clause could possibly be used for a :has_key operation
    #  w/ a little more work (e.g. does teams object {} have "bears" key:
    #  (teams,:any,(,:eq,"bears"))) (jsonb_object_keys
    #  https://www.postgresql.org/docs/9.5/functions-json.html#FUNCTIONS-JSON-OP-TABLE)
    def get_any_sql_clause(target_json_object, scalar_operator_name, scalar_other):
        if (
            "type" in operators[scalar_operator_name]
            and operators[scalar_operator_name]["type"] == "text"
        ):
            f = db.func.jsonb_array_elements_text
        else:
            f = db.func.jsonb_array_elements
            scalar_other = cast(scalar_other, JSONB)

        f = f(target_json_object).alias()

        return exists(
            #  XXX select 1
            db.select("*")
            .select_from(f)
            .where(
                operators[scalar_operator_name]["sql_comparator"](
                    column(f.name), scalar_other
                )
            )
        )

    def get_all_sql_clause(target_json_object, scalar_operator_name, scalar_other):
        #  XXX should DRY duplication between get_any_sql_clause and
        #  get_all_sql_clause functions
        if (
            "type" in operators[scalar_operator_name]
            and operators[scalar_operator_name]["type"] == "text"
        ):
            f = db.func.jsonb_array_elements_text
        else:
            f = db.func.jsonb_array_elements
            scalar_other = cast(scalar_other, JSONB)

        f = f(target_json_object).alias()
        count_f = db.func.jsonb_array_length(target_json_object)

        return ColumnOperators.__eq__(
            count_f,
            db.select([db.func.count()])
            .select_from(f)
            .where(
                operators[scalar_operator_name]["sql_comparator"](
                    column(f.name), scalar_other
                )
            ),
        )

    #  XXX aggregate operators might be nice (e.g size, max, etc.)
    operators = {
        ":eq": {"sql_comparator": ColumnOperators.__eq__},
        ":ne": {"sql_comparator": ColumnOperators.__ne__},
        ":gt": {"sql_comparator": ColumnOperators.__gt__},
        ":gte": {"sql_comparator": ColumnOperators.__ge__},
        ":lt": {"sql_comparator": ColumnOperators.__lt__},
        ":lte": {"sql_comparator": ColumnOperators.__le__},
        #  XXX :is is not working
        #  XXX what does SQL IS mean?
        #  XXX check how mongoDB filters for null (does it use it?, if an object
        #  doesn't have a given key, is that considered null? etc.)
        ":is": {"sql_comparator": ColumnOperators.is_},
        #  XXX :in is probably unnecessary (i.e. instead of (score, :in, [1, 2]) do (or(score,:eq,1),(score,:eq,2)))
        #  ":in": ColumnOperators.in_,
        ":like": {
            "type": "text",
            "sql_comparator": ColumnOperators.like,
        },
        ":all": {"sql_clause": get_all_sql_clause},
        ":any": {"sql_clause": get_any_sql_clause},
    }

    def parse_filter(filter_string):
        if not filter_string:
            return

        #  https://github.com/erikrose/parsimonious/pull/23/files
        grammar = Grammar(
            """
        filter           = scalar_filter / boolean_filter
        boolean_filter   = open boolean boolean_operands close
        boolean          = "and" / "or"
        boolean_operands = boolean_operand+
        boolean_operand = separator scalar_filter_or_boolean_filter
        scalar_filter_or_boolean_filter = scalar_filter / boolean_filter
        scalar_filter   = open json_key separator operator separator json_value_or_scalar_filter close
        json_value_or_scalar_filter = json_value / scalar_filter
        json_key        = ~"[A-Z 0-9_.]*"i
        operator        = ":eq" / ":ne" / ":gt" / ":gte" / ":lt" / ":lte" / ":is" / ":like" / ":all" / ":any"
        open            = "("
        close           = ")"
        separator       = ","

        json_value = true_false_null / number / doubleString
        true_false_null = "true" / "false" / "null"
        number = (int frac exp) / (int exp) / (int frac) / int
        int = "-"? ((digit1to9 digits) / digit)
        frac = "." digits
        exp = e digits
        digits = digit+
        e = "e+" / "e-" / "e" / "E+" / "E-" / "E"
        digit1to9 = ~"[1-9]"
        digit = ~"[0-9]"

        doubleString   = ~'"([^"])*"'
        """
        )
        return grammar.parse(filter_string)

    class FilterVisitor(NodeVisitor):
        def visit_filter(self, node, visited_children):
            return visited_children[0]

        def visit_boolean_filter(self, node, visited_children):
            _, boolean, boolean_operands, _ = visited_children
            return {boolean: boolean_operands}

        def visit_boolean(self, node, visited_children):
            return node.text

        def visit_boolean_operand(self, node, visited_children):
            _, scalar_filter_or_boolean_filter = visited_children
            return scalar_filter_or_boolean_filter

        def visit_scalar_filter_or_boolean_filter(self, node, visited_children):
            return visited_children[0]

        def visit_scalar_filter(self, node, visited_children):
            (
                _,
                json_key,
                _,
                operator,
                _,
                json_value_or_scalar_filter,
                _,
            ) = visited_children

            return {
                "name": json_key,
                "op": operator,
                "val": json_value_or_scalar_filter,
            }

        def visit_json_value_or_scalar_filter(self, node, visited_children):
            return visited_children[0]

        def visit_json_key(self, node, visited_children):
            return node.text

        def visit_operator(self, node, visited_children):
            return node.text

        def visit_json_value(self, node, visited_children):
            return json.loads(node.text)

        def generic_visit(self, node, visited_children):
            return visited_children or node

    def get_clause(filter_dict):
        if not filter_dict:
            return

        if len(filter_dict) == 1:
            boolean = list(filter_dict.keys())[0]

            boolean_operand_clauses = (get_clause(c) for c in filter_dict[boolean])
            #  XXX define these boolean operators in one place
            if boolean == "and":
                return db.and_(boolean_operand_clauses)
            elif boolean == "or":
                return db.or_(boolean_operand_clauses)
            raise

        return add_filter(filter_dict["name"], filter_dict["op"], filter_dict["val"])

    filter_tree = parse_filter(filter)
    filter_dict = {}
    if filter_tree:
        filter_tree_visitor = FilterVisitor()
        filter_dict = filter_tree_visitor.visit(filter_tree)

    if data:
        query = Metadata.query
        if filter_dict:
            query = query.where(get_clause(filter_dict))
        return {metadata.guid: metadata.data for metadata in await query.gino.all()}
    else:
        query = db.select([Metadata.guid])
        if filter_dict:
            query = query.where(get_clause(filter_dict))
        return [row[0] for row in await query.gino.return_model(False).all()]


@mod.get("/metadata/{guid:path}")
async def get_metadata(guid):
    """Get the metadata of the GUID."""
    metadata = await Metadata.get(guid)
    if metadata:
        return metadata.data
    else:
        raise HTTPException(HTTP_404_NOT_FOUND, f"Not found: {guid}")


def init_app(app):
    app.include_router(mod, tags=["Query"])
