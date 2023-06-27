from data import *
from typing import cast

class MrGen:
  '''
  middle representation generator
  '''

  def __init__(self, unit) -> None:
    from unit import TranslationUnit

    self.unit: TranslationUnit = unit

  @property
  def root(self) -> MultipleNode:
    return self.unit.root

  @property
  def tab(self) -> SemaTable:
    return self.unit.tab

  def get_declaration_name(self, node: Node | None) -> str:
    if isinstance(node, Token) and node.kind == 'id':
      return cast(str, node.value)

    if not isinstance(node, SyntaxNode):
      raise UnreachableError()

    match node.syntax_name:
      case 'Declarator':
        return self.get_declaration_name(node['direct_declarator'])

      case                     \
        'ArrayDeclarator'    | \
        'Declaration'        | \
        'FunctionDefinition' | \
        'ParameterListDeclarator':
          return self.get_declaration_name(node['declarator'])

      case _:
        raise UnreachableError()

  def create_numeric_typ_from_dspecs(
    self,
    loc: Loc,
    specs: dict[str, int],
    typedefed: Token | None
  ) -> Typ:
    if typedefed is not None:
      raise NotImplementedError()

    # awkward pythonic type notation
    combinations: list[tuple[str, dict[str, int | tuple[int, ...]]]]
    combinations = [
      ('void',     {'void': 1}),
      ('char',     {'char': 1}),
      ('int',      {'int': 1}),
      ('short',    {'short': 1, 'int': (0, 1)}),
      ('long',     {'long': 1, 'int': (0, 1)}),
      ('longlong', {'long': 2, 'int': (0, 1)}),
    ]

    def match_combination(comb: dict[str, int | tuple[int, ...]]) -> bool:
      for kind, count in comb.items():
        is_option = isinstance(count, tuple)
        option = cast(tuple[int, ...], count)

        if kind not in specs:
          if is_option and 0 in option:
            continue

          return False

        if is_option and specs[kind] not in option:
          return False

        if isinstance(count, int) and specs[kind] != count:
          return False

      return True

    for kind, comb in combinations:
      if not match_combination(comb): # type: ignore[arg-type]
        continue

      if kind == 'void':
        return VoidTyp()

      return IntTyp(kind, 'unsigned' not in comb)

    self.unit.report('invalid combination of numeric types', loc)
    return PoisonedTyp()

  def create_typ_from_dspecs(self, dspecs: MultipleNode) -> Typ:
    from z9_dparser import TYPE_QUALS, TYPE_SPECS

    loc = dspecs.nodes[0].loc
    quals: set[str] = set()
    specs: dict[str, int] = {}
    typedefed: Token | None = None

    for dspec in dspecs.nodes:
      # not always true,
      # but at the moment it's okay
      assert isinstance(dspec, Token)
      k = dspec.kind

      if k in TYPE_QUALS:
        quals.add(k)
        continue

      if k in TYPE_SPECS:
        if k not in specs:
          specs[k] = 0

        specs[k] += 1
        continue

      if k == 'id':
        assert typedefed is None
        typedefed = dspec
        continue

      raise UnreachableError()

    if len(specs) == 0 and typedefed is None:
      self.unit.report('incomplete type', loc)
      return PoisonedTyp()

    typ = self.create_numeric_typ_from_dspecs(loc, specs, typedefed)

    self.apply_typequals_on_typ(typ, quals)

    return typ

  def apply_typequals_on_typ(self, typ: Typ, quals: set[str]) -> None:
    for qual in quals:
      match qual:
        case 'const':
          typ.is_const = True

        case _:
          raise UnreachableError()

  def type_qualifier_list_to_quals(self, qual_list: MultipleNode) -> set[str]:
    return set(
      # not always true,
      # but at the moment it's okay
      cast(Token, q).kind
        for q in qual_list.nodes
    )

  def make_fntyp_from_param_list_declarator(
    self,
    ret: Typ,
    node: SyntaxNode
  ) -> Typ:
    # TODO: implement ellipsis
    params = cast(MultipleNode, node['parameter_list'])
    typs: list[Typ] = [
      self.get_declaration_typ(p)
        for p in params.nodes
    ]

    return FnTyp(ret, typs)

  def make_typ_from_declarator(self, typ: Typ, d: Node | None) -> Typ:
    if not isinstance(d, SyntaxNode):
      return typ

    match d.syntax_name:
      case 'ArrayDeclarator':
        return self.make_typ_from_declarator(
          # TODO: add the evaluated size initializer
          ArrayTyp(typ, POISONED_VAL), d['declarator']
        )

      case 'ParameterListDeclarator':
        typ = self.make_fntyp_from_param_list_declarator(
          typ, d
        )

        return self.make_typ_from_declarator(
          typ, d['declarator']
        )

      case 'Pointer':
        typ = self.make_typ_from_declarator(
          typ, d['pointer']
        )

        typ = PointerTyp(typ)

        self.apply_typequals_on_typ(
          typ,
          self.type_qualifier_list_to_quals(
            cast(MultipleNode, d['type_qualifier_list'])
          )
        )

        return typ

      case 'Declarator':
        # for the pointer
        typ = self.make_typ_from_declarator(
          typ, d['pointer']
        )

        # recursively (maybe there is the array too)
        typ = self.make_typ_from_declarator(
          typ, d['direct_declarator']
        )

        return typ

      case _:
        return typ
    
  def get_declaration_typ(self, node: Node | None) -> Typ:
    if isinstance(node, MultipleNode):
      return self.create_typ_from_dspecs(node)

    if not isinstance(node, SyntaxNode):
      raise UnreachableError()

    match node.syntax_name:
      case \
        'Declaration'        | \
        'FunctionDefinition' | \
        'ParameterDeclaration':
          typ = self.get_declaration_typ(node['declaration_specifiers'])
          typ = self.make_typ_from_declarator(typ, node['declarator'])
          return typ

      case _:
        raise UnreachableError()

  def predeclare_top_level(self, node: Node) -> None:
    if node.is_empty_decl():
      raise NotImplementedError()

    assert isinstance(node, SyntaxNode)

    key = {
      'Declaration': 'initializer',
      'FunctionDefinition': 'body'
    }[node.syntax_name]
    
    name = self.get_declaration_name(node)
    is_weak = node[key] is None
    print(name)

    self.tab.declare(name, node, is_weak, node.loc)

  def process_top_level(self, node: Node) -> None:
    # TODO: for FunctionDefinitions should produce
    #       a function pointer type
    #       so that i can compare it easily
    typ = self.get_declaration_typ(node)
    print(typ)

  def gen_whole_unit(self) -> None:
    for top_level in self.root.nodes:
      self.predeclare_top_level(top_level)

    for sym, is_weak in self.tab.members.values():
      # TODO: check that all weak declarations
      #       match with the non-weak one
      if is_weak:
        continue

      self.process_top_level(cast(Node, sym))