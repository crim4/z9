from copy import copy
from data import *
from llvmlite import ir as ll

class MrChip:
  def __init__(self, unit) -> None:
    from unit import TranslationUnit

    self.unit: TranslationUnit = unit

    self.sym: Symbol
    self.llfn: ll.Function
    self.allocas: ll.IRBuilder
    self.code: ll.IRBuilder
    self.stack: list[Val] = []
    self.memories: list[SymTable] = [self.unit.tab]
    self.control_flow_is_terminated: bool = False

  @property
  def fntyp(self) -> FnTyp:
    return cast(FnTyp, self.sym.typ)

  @property
  def mem(self) -> SymTable:
    return self.memories[-1]

  def push_mem(self) -> None:
    self.memories.append(self.mem.copy())

  def pop_mem(self) -> None:
    self.memories.pop()

  def create_numeric_typ_from_dspecs(
    self,
    loc: Loc,
    specs: dict[str, int],
    typedefed: Token | None
  ) -> Typ:
    if typedefed is not None:
      raise NotImplementedError()

    # awkward pythonic type notation
    combinations: list[tuple[str, dict[str, int]]]
    # https://en.wikipedia.org/wiki/C_data_types
    combinations = [
      ('void',     {'void': 1}),

      ('char',     {'char': 1}),

      ('int',      {'int': 1}),
      
      ('short',    {'short': 1}),
      ('short',    {'short': 1, 'int': 1}),

      ('long',     {'long': 1}),
      ('long',     {'long': 1, 'int': 1}),

      ('longlong', {'long': 2}),
      ('longlong', {'long': 2, 'int': 1}),
    ]

    def match_combination(comb: dict[str, int]) -> bool:
      if len(comb) != len(specs):
        return False

      for kind, count in comb.items():
        if kind not in specs:
          return False

        if specs[kind] != count:
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
    from z9_gen import get_declaration_name

    # TODO: implement ellipsis
    params = cast(MultipleNode, node['parameter_list'])

    typs: list[Typ] = [
      self.get_declaration_typ(p)
        for p in params.nodes
    ]
    names: list[Token | None] = [
      get_declaration_name(p)
        for p in params.nodes
    ]

    return FnTyp(ret, typs, names)

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

  def llcast(self, source: ll.Value, target: ll.Type, is_trunc: bool) -> ll.Value:
    if is_trunc:
      return self.code.trunc(source, target) # type:ignore
    
    return self.code.sext(source, target) # type:ignore

  def maybe_coerce(self, expected: Typ, actual_val: Val) -> Typ:
    actual = actual_val.typ
    typs  = [type(expected), type(actual)]

    if isinstance(actual, LitIntTyp):
      if isinstance(expected, IntTyp):
        return expected

      # the default type for literal ints
      # is always `int`
      return IntTyp('int', True)
    
    se = expected.bit_size()
    sa = actual.bit_size()
    
    # TODO: ban this when inside `strict_rules`
    if typs == [IntTyp, IntTyp] and sa != se:
      if expected.kind == '_Bool': # type:ignore
        actual_val.llv = self.code.icmp_signed(
          '!=',
          actual_val.llv,
          ll.Constant(typ_to_lltyp(actual_val.typ), 0)
        )
        return expected
      
      actual_val.llv = self.llcast(actual_val.llv, typ_to_lltyp(expected), sa > se)
      return expected
    
    return actual

  def typcheck(self, expected: Typ, actual_val: Val, l: Loc) -> tuple[bool, Typ]:
    actual = self.maybe_coerce(expected, actual_val)

    if expected == actual:
      return (True, actual)

    self.unit.report(
      f'expected type "{expected}", got "{actual}"', l
    )
    return (False, expected)

  def maybe_coerce_binary(self, l_val: Val, r_val: Val) -> tuple[Typ, Typ]:
    l, r = l_val.typ, r_val.typ

    typs  = [type(l), type(r)]
    vals  = [l_val, r_val]
    get_t = lambda t: [l, r][typs.index(t)]

    if LitIntTyp in typs and IntTyp in typs:
      meta_val = vals[typs.index(LitIntTyp)]
      meta_val.typ = get_t(IntTyp)
      meta_val.llv = ll.Constant(typ_to_lltyp(get_t(IntTyp)), meta_val.meta)

      return (get_t(IntTyp), get_t(IntTyp))
    
    ls = l.bit_size()
    rs = r.bit_size()

    if typs == [IntTyp, IntTyp] and ls != rs:
      if ls > rs:
        r_val.llv = self.llcast(r_val.llv, typ_to_lltyp(l), False)
        promoted = l
      else:
        l_val.llv = self.llcast(l_val.llv, typ_to_lltyp(r), False)
        promoted = r

      return (promoted, promoted)

    return (l, r)

  def typcheck_binary(self, l_val: Val, r_val: Val, opcode: Opcode, loc: Loc) -> tuple[bool, Val, Val]:
    l, r = self.maybe_coerce_binary(l_val, r_val)

    if l == r:
      return (True, l_val, r_val)

    op = ARITHMETIC_OPCODES.inverse[opcode]
    self.unit.report(
      f'types "{l}" and "{r}" are not compatible for binary operator "{op}"',
      loc
    )
    return (True, l_val, r_val)

  def get_one_runtime_typ(self, l: Val, r: Val) -> Typ:
    if not l.is_meta():
      return l.typ

    return r.typ

  def perform_meta_op(self, op: Opcode, l: Any, r: Any) -> Any:
    match op:
      case Opcode.ADD: return l + r
      case Opcode.SUB: return l - r
      case Opcode.MUL: return l * r

      case _:
        raise UnreachableError()

  def fold_meta(self, op: Opcode, l: Val, r: Val) -> Val | None:
    if not l.is_meta() or not r.is_meta():
      return None

    # TODO: is this okay?
    #       i think it's broken
    if l.typ != r.typ:
      return None

    res = copy(l)
    res.meta = self.perform_meta_op(op, l.meta, r.meta)
    res.llv.constant = l.meta # type:ignore
    return l
  
  def push(self, val: Val) -> None:
    self.stack.append(val)

  def pop(self) -> Val:
    return self.stack.pop()

  def opcode_to_llop(self, op: Opcode) -> Callable:
    return {
      Opcode.ADD: self.code.add,
      Opcode.SUB: self.code.sub,
      Opcode.MUL: self.code.mul,
      Opcode.REM: self.code.srem,
      Opcode.DIV: self.code.sdiv,
      Opcode.SHL: self.code.shl,
      # TODO: Opcode.SHR: self.code.shr,
      # TODO: Opcode.LT:  self.code.cmp,
      #       Opcode.GT:  self.code.gt,
      #       Opcode.LET: self.code.let,
      #       Opcode.GET: self.code.get,
      #       Opcode.EQ:  self.code.eq,
      #       Opcode.NEQ: self.code.neq,
      Opcode.AND: self.code.and_,
      Opcode.XOR: self.code.xor,
      Opcode.OR:  self.code.or_,
    }[op]

  def fold_meta_or_typcheck_binary(self, op: Opcode, l: Val, r: Val, loc: Loc) -> None:
    if (binary := self.fold_meta(op, l, r)) is not None:
      self.push(binary)
      return

    is_ok, l, r = self.typcheck_binary(l, r, op, loc)
    op_typ = self.get_one_runtime_typ(l, r)
    if is_ok:
      llv = self.opcode_to_llop(op)(l.llv, r.llv)
    else:
      llv = llundef(op_typ)

    self.push(Val(op_typ, None, llv))
  
  def terminate_control_flow(self) -> None:
    self.control_flow_is_terminated = True
  
  def reopen_control_flow(self) -> None:
    self.control_flow_is_terminated = False

  def process_instr(self, block_labels: dict[int, ll.Block], label: int, i: Instr) -> None:
    if label in block_labels:
      new_block = block_labels.pop(label)
      self.jump_to_new_block_if_needed(new_block)

      self.code = ll.IRBuilder(new_block)
      self.reopen_control_flow()
    
    if self.control_flow_is_terminated:
      return

    match i.op:
      case Opcode.RET_VOID:
        is_ok, _ = self.typcheck(self.fntyp.ret, Val(VoidTyp()), i.loc)

        if not is_ok:
          return
        
        self.code.ret_void()
        self.terminate_control_flow()

      case Opcode.RET:
        v = self.pop()
        is_ok, v.typ = self.typcheck(self.fntyp.ret, v, i.loc)

        if not is_ok:
          return
        
        self.code.ret(v.llv)
        self.terminate_control_flow()

      # TODO: implement also operators
      #       for user-defined types
      case Opcode.ADD | Opcode.SUB | Opcode.MUL:
        r = self.pop()
        l = self.pop()

        self.fold_meta_or_typcheck_binary(i.op, l, r, i.loc)

      case Opcode.LOAD_NAME:
        name = cast(str, i.ex)
        m = self.mem.get_member(name, i.loc)

        # TODO: add the ability to get non local symbols
        #       such as functions
        if isinstance(m, LocalSymbol):
          llv = self.code.load(m.llv)
          self.push(Val(m.typ, llv=llv))
        else:
          self.push(POISONED_VAL)
      
      case Opcode.JUMP_IF_FALSE:
        v = self.pop()
        is_ok, v.typ = self.typcheck(IntTyp('_Bool', True), v, i.loc)

        if v.is_meta():
          if v.meta:
            self.terminate_control_flow()
            block_labels[label + 1] = self.code.block # type:ignore
          else:
            self.terminate_control_flow()
            block_labels[i.ex] = self.code.block # type:ignore

          return

        continue_block, jump_block = self.create_fork_blocks(label + 1, i.ex)

        if not is_ok:
          return
        
        self.code.cbranch(v.llv, continue_block, jump_block)
        self.code = ll.IRBuilder(continue_block)

        block_labels[i.ex] = jump_block

      case Opcode.JUMP:
        jump_block = self.llfn.append_basic_block(f'L{i.ex}')
        block_labels[i.ex] = jump_block
        self.terminate_control_flow()

      case Opcode.PUSH:
        self.push(i.ex)

      case _:
        raise UnreachableError(repr(i.op))

  def create_fork_blocks(self, label1: int, label2: int) -> tuple[ll.Block, ll.Block]:
    l1, l2 = f'L{label1}', f'L{label2}'
    
    '''
    block1 = None
    block2 = None

    for block in self.llfn.blocks:
      if block.name == l1:
        block1 = block
      elif block.name == l2:
        block2 = block

    if block1 is None:
      block1 = self.llfn.append_basic_block(l1)
    
    if block2 is None:
      block2 = self.llfn.append_basic_block(l2)
    '''

    block1 = self.llfn.append_basic_block(l1)
    block2 = self.llfn.append_basic_block(l2)

    return block1, block2

  def jump_to_new_block_if_needed(self, new_block: ll.Block) -> None:
    if self.control_flow_is_terminated:
      return

    # probably never possible
    if self.code.block is None:
      return

    if self.code.block.is_terminated:
      return
    
    self.code.branch(new_block)

  def config_fn(self, sym: FnSymbol) -> None:
    sym.typ = self.get_declaration_typ(sym.fn.node)
    self.sym = sym

    self.llfn = ll.Function(self.unit.llmod, typ_to_lltyp(self.fntyp), sym.name)
    self.allocas = ll.IRBuilder(self.llfn.append_basic_block('allocas'))
    self.code = ll.IRBuilder(self.llfn.append_basic_block('entry'))

    self.allocas.branch(self.code.block)
    self.allocas.position_at_start(self.allocas.block)

  def process_fnsym(self, sym: FnSymbol) -> None:
    self.config_fn(sym)
    self.push_mem()

    # declaring parameters as locals
    for i, (ptyp, pname) in enumerate(zip(self.fntyp.params, self.fntyp.pnames)):
      if pname is None:
        continue
        
      param_name = cast(str, pname.value)

      palloca = self.allocas.alloca(typ_to_lltyp(ptyp), name=param_name)
      self.code.store(self.llfn.args[i], palloca)

      self.mem.declare_local(
        param_name,
        ptyp,
        palloca,
        pname.loc
      )

    block_labels: dict[int, ll.Block] = {}

    for label, i in enumerate(sym.fn.code.instructions):
      self.process_instr(block_labels, label, i)
    
    self.cleanup_empty_blocks()

    self.emit_implicit_return()
    self.pop_mem()
  
  def cleanup_empty_blocks(self) -> None:
    for block in self.llfn.blocks:
      if len(block.instructions) == 0:
        self.llfn.blocks.remove(block)
  
  def emit_implicit_return(self) -> None:
    # probably never possible
    if self.code.block is None:
      return
    
    if self.code.block.is_terminated:
      return

    if isinstance(self.fntyp.ret, VoidTyp):
      self.code.ret_void()
      return
    
    self.code.ret(llundef(self.fntyp.ret))
    self.unit.warn('not all paths of the function return', self.sym.loc)

  def process_sym(self, sym: Symbol) -> None:
    if not isinstance(sym, FnSymbol):
      raise NotImplementedError()

    self.process_fnsym(sym)

  def process_whole_tab(self) -> None:
    for sym in self.unit.tab.members.values():
      self.process_sym(cast(Symbol, sym))