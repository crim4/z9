from data import *
from llvmlite import ir as ll
from llvmlite.binding import targets as lltargets, initialize as llinitialize
from subprocess import run as runprocess
from sys import argv

class TranslationUnit:
  def __init__(self, filepath: str) -> None:
    from tempfile import NamedTemporaryFile
    from rich.console import Console

    self.console = Console(emoji=False)

    preprocessed_filepath: str = NamedTemporaryFile().name
    clang_cpp = runprocess(
      f'clang-cpp.exe -std=c99 -nostdinc -Iinclude "{filepath}" -o "{preprocessed_filepath}"'
    ).returncode

    self.failed = clang_cpp != 0

    self.errors: list[tuple[str, Loc]] = []
    self.warnings: list[tuple[str, Loc]] = []

    if self.failed:
      return

    self.filepath: str = filepath
    self.source: str = open(preprocessed_filepath, 'r').read()

    llinitialize()

    self.llmod: ll.Module = ll.Module(filepath)
    self.llmod.triple = lltargets.get_process_triple()

  def fix_message(self, m: str) -> str:
    return m.replace('[', '\\[')

  def compile(self) -> None:
    if self.failed:
      return
    
    if '-exe' not in argv:
      return
    
    from os.path import splitext
    from os import remove as removefile
    
    lloutput = splitext(self.filepath)[0] + '.ll'
    exeoutput = splitext(self.filepath)[0] + '.exe'

    open(lloutput, 'w').write(repr(self.llmod))

    self.failed = runprocess(
      f'clang.exe "{lloutput}" -o "{exeoutput}" -Wno-override-module'
    ).returncode != 0

    removefile(lloutput)

  def print_diagnostic(self) -> None:
    dprint = lambda m, l, color: \
      self.console.print(
        f'[b][{color}]{l}[/{color}][/b]: {self.fix_message(m)}'
      )

    for message, loc in self.errors:
      dprint(message, loc, 'red')

    for message, loc in self.warnings:
      dprint(message, loc, 'purple')

  def report(self, message: str, loc: Loc) -> None:
    self.errors.append((message, loc))

  def warn(self, message: str, loc: Loc) -> None:
    self.warnings.append((message, loc))

  def lex(self) -> None:
    from z9_lexer import Lexer
    from data import Token

    if self.failed:
      return

    self.tokens: list[Token] = []
    l = Lexer(self)

    while l.has_char():
      token: Token | None = l.next_token()

      if token is None:
        break

      self.tokens.append(token)

  def mrgen(self) -> None:
    from z9_gen import MrGen
    from data import SymTable

    if self.failed:
      return

    g = MrGen(self)
    self.tab: SymTable = SymTable(self)

    g.gen_whole_unit()
    self.maybe_mark_as_failed()

  def mrchip(self) -> None:
    from z9_chip import MrChip

    if self.failed:
      return

    c = MrChip(self)
    c.process_whole_tab()
    self.maybe_mark_as_failed()

  def dparse(self) -> None:
    from z9_dparser import DParser
    from data import MultipleNode

    if self.failed:
      return

    if len(self.tokens) == 0:
      self.root: MultipleNode = MultipleNode(Loc(self.filepath, 1, 1))
      return

    d = DParser(self)
    self.root = MultipleNode(d.cur.loc)

    try:    
      # the top level scope behaves the same as a
      # struct's or union's body, except that functions
      # cannot have method modifiers (such as `t f() const|static {}`)
      d.struct_or_union_declaration_list_into(
        self.root, expect_braces=False, allow_method_mods=False
      )
    except ParsingError:
      self.failed = True

    self.maybe_mark_as_failed()

  def maybe_mark_as_failed(self) -> None:
    self.failed = self.failed or len(self.errors) > 0

  def dump_root(self) -> None:
    if self.failed:
      return

    self.console.print(
      self.fix_message(repr(self.root))
    )

  def dump_tab(self) -> None:
    if self.failed:
      return

    self.console.print(
      self.fix_message(repr(self.tab))
    )

  def dump_llmod(self) -> None:
    if self.failed:
      return

    self.console.print(
      self.fix_message(repr(self.llmod))
    )