from __future__ import annotations

import pathlib
import uuid
from contextlib import contextmanager
from importlib import import_module
from os import PathLike
from typing import Any, ContextManager, Dict, Hashable, Set, Union

from jschon.exceptions import CatalogError, JSONPointerError, URIError
from jschon.json import JSONCompatible
from jschon.jsonpointer import JSONPointer
from jschon.jsonschema import JSONSchema
from jschon.uri import URI
from jschon.utils import json_loadf, json_loadr
from jschon.vocabulary import KeywordClass, Metaschema, Vocabulary

__all__ = [
    'Catalog',
    'Source',
    'LocalSource',
    'RemoteSource',
]


class Source:
    def __init__(self, suffix: str = None) -> None:
        self.suffix = suffix

    def __call__(self, relative_path: str) -> JSONCompatible:
        raise NotImplementedError


class LocalSource(Source):
    def __init__(self, base_dir: Union[str, PathLike], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.base_dir = base_dir

    def __call__(self, relative_path: str) -> JSONCompatible:
        filepath = pathlib.Path(self.base_dir) / relative_path
        if self.suffix:
            filepath = str(filepath)
            filepath += self.suffix

        return json_loadf(filepath)


class RemoteSource(Source):
    def __init__(self, base_url: URI, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.base_url = base_url

    def __call__(self, relative_path: str) -> JSONCompatible:
        url = str(URI(relative_path).resolve(self.base_url))
        if self.suffix:
            url += self.suffix

        return json_loadr(url)


class Catalog:
    """The :class:`Catalog` acts as a schema cache, enabling schemas and
    subschemas to be indexed, re-used, and cross-referenced by URI."""

    _catalog_registry: Dict[Hashable, Catalog] = {}

    @classmethod
    def get_catalog(cls, name: str = 'catalog') -> Catalog:
        try:
            return cls._catalog_registry[name]
        except KeyError:
            raise CatalogError(f'Catalog name "{name}" not found.')

    def __init__(self, name: str = 'catalog') -> None:
        """Initialize a :class:`Catalog` instance.

        :param name: a unique name for this :class:`Catalog` instance
        """
        self.__class__._catalog_registry[name] = self

        self.name: str = name
        """The unique name of this :class:`Catalog` instance."""

        self._uri_sources: Dict[URI, Source] = {}
        self._vocabularies: Dict[URI, Vocabulary] = {}
        self._schema_cache: Dict[Hashable, Dict[URI, JSONSchema]] = {}
        self._enabled_formats: Set[str] = set()

    def add_uri_source(self, base_uri: URI, source: Source):
        """Register a source for URI-identified JSON resources.

        :param base_uri: a normalized, absolute URI - including scheme, without
            a fragment, and ending with ``'/'``
        :param source: a :class:`Source` object
        :raise CatalogError: if `base_uri` is invalid
        """
        try:
            base_uri.validate(require_scheme=True, require_normalized=True, allow_fragment=False)
        except URIError as e:
            raise CatalogError from e

        if not base_uri.path or not base_uri.path.endswith('/'):
            raise CatalogError('base_uri must end with "/"')

        self._uri_sources[base_uri] = source

    def load_json(self, uri: URI) -> JSONCompatible:
        """Load a JSON-compatible object from the source for `uri`.

        If there are multiple candidate base URIs for `uri`, the most specific
        match (i.e. the longest one) is selected.

        :param uri: a normalized, absolute URI - including scheme, without
            a fragment
        :raise CatalogError: if `uri` is invalid, a source is not available
            for `uri`, or if a loading error occurs
        """
        try:
            uri.validate(require_scheme=True, require_normalized=True, allow_fragment=False)
        except URIError as e:
            raise CatalogError from e

        uristr = str(uri)
        candidates = [
            (base_uristr, source)
            for base_uri, source in self._uri_sources.items()
            if uristr.startswith(base_uristr := str(base_uri))
        ]
        if candidates:
            candidates.sort(key=lambda c: len(c[0]), reverse=True)
            base_uristr, source = candidates[0]
            relative_path = uristr[len(base_uristr):]
            try:
                return source(relative_path)
            except Exception as e:
                raise CatalogError(*e.args) from e

        raise CatalogError(f'A source is not available for "{uri}"')

    def create_vocabulary(self, uri: URI, *kwclasses: KeywordClass) -> None:
        """Create a :class:`~jschon.vocabulary.Vocabulary` object, which
        may be used by a :class:`~jschon.vocabulary.Metaschema` to provide
        keyword classes used in schema construction.

        :param uri: the URI identifying the vocabulary
        :param kwclasses: the :class:`~jschon.vocabulary.Keyword` classes
            constituting the vocabulary
        """
        self._vocabularies[uri] = Vocabulary(uri, *kwclasses)

    def get_vocabulary(self, uri: URI) -> Vocabulary:
        """Get a :class:`~jschon.vocabulary.Vocabulary` by its `uri`.

        :param uri: the URI identifying the vocabulary
        :raise CatalogError: if `uri` is not a recognized vocabulary URI
        """
        try:
            return self._vocabularies[uri]
        except KeyError:
            raise CatalogError(f"Unrecognized vocabulary URI '{uri}'")

    def create_metaschema(
            self,
            uri: URI,
            core_vocabulary_uri: URI,
            *default_vocabulary_uris: URI,
            **kwargs: Any,
    ) -> None:
        """Create, cache and validate a :class:`~jschon.vocabulary.Metaschema`.

        :param uri: the URI identifying the metaschema
        :param core_vocabulary_uri: the URI identifying the metaschema's
            core :class:`~jschon.vocabulary.Vocabulary`
        :param default_vocabulary_uris: default :class:`~jschon.vocabulary.Vocabulary`
            URIs, used in the absence of a ``"$vocabulary"`` keyword in the
            metaschema JSON file
        :param kwargs: additional keyword arguments to pass through to the
            :class:`~jschon.jsonschema.JSONSchema` constructor
        """
        metaschema_doc = self.load_json(uri)
        core_vocabulary = self.get_vocabulary(core_vocabulary_uri)
        default_vocabularies = [
            self.get_vocabulary(vocab_uri)
            for vocab_uri in default_vocabulary_uris
        ]
        metaschema = Metaschema(
            self,
            metaschema_doc,
            core_vocabulary,
            *default_vocabularies,
            **kwargs,
            uri=uri,
        )
        if not metaschema.validate().valid:
            raise CatalogError("The metaschema is invalid against itself")

    def enable_formats(self, *format_attr: str) -> None:
        """Enable validation of the specified format attributes.

        These may include formats defined in :mod:`jschon.formats`
        and elsewhere.
        """
        import_module('jschon.formats')
        self._enabled_formats |= set(format_attr)

    def is_format_enabled(self, format_attr) -> bool:
        """Return True if validation is enabled for `format_attr`,
        False otherwise."""
        return format_attr in self._enabled_formats

    def add_schema(
            self,
            uri: URI,
            schema: JSONSchema,
            *,
            cacheid: Hashable = 'default',
    ) -> None:
        """Add a (sub)schema to a cache.

        :param uri: the URI identifying the (sub)schema
        :param schema: the :class:`~jschon.jsonschema.JSONSchema` instance to cache
        :param cacheid: schema cache identifier
        """
        self._schema_cache.setdefault(cacheid, {})
        self._schema_cache[cacheid][uri] = schema

    def del_schema(
            self,
            uri: URI,
            *,
            cacheid: Hashable = 'default',
    ) -> None:
        """Remove a (sub)schema from a cache.

        :param uri: the URI identifying the (sub)schema
        :param cacheid: schema cache identifier
        """
        if cacheid in self._schema_cache:
            self._schema_cache[cacheid].pop(uri, None)

    def get_schema(
            self,
            uri: URI,
            *,
            metaschema_uri: URI = None,
            cacheid: Hashable = 'default',
    ) -> JSONSchema:
        """Get a (sub)schema identified by `uri` from a cache, or
        load it from disk if not already cached.

        :param uri: the URI identifying the (sub)schema
        :param metaschema_uri: passed to the :class:`~jschon.jsonschema.JSONSchema`
            constructor when loading a new instance from disk
        :param cacheid: schema cache identifier
        :raise CatalogError: if a schema cannot be found for `uri`, or if the
            object referenced by `uri` is not a :class:`~jschon.jsonschema.JSONSchema`
        """
        try:
            return self._schema_cache[cacheid][uri]
        except KeyError:
            pass

        schema = None
        base_uri = uri.copy(fragment=False)

        if uri.fragment is not None:
            try:
                schema = self._schema_cache[cacheid][base_uri]
            except KeyError:
                pass

        if schema is None:
            doc = self.load_json(base_uri)
            schema = JSONSchema(
                doc,
                catalog=self,
                cacheid=cacheid,
                uri=base_uri,
                metaschema_uri=metaschema_uri,
            )
            try:
                return self._schema_cache[cacheid][uri]
            except KeyError:
                pass

        if uri.fragment:
            try:
                ptr = JSONPointer.parse_uri_fragment(uri.fragment)
                schema = ptr.evaluate(schema)
            except JSONPointerError as e:
                raise CatalogError(f"Schema not found for {uri}") from e

        if not isinstance(schema, JSONSchema):
            raise CatalogError(f"The object referenced by {uri} is not a JSON Schema")

        return schema

    @contextmanager
    def cache(self, cacheid: Hashable = None) -> ContextManager[Hashable]:
        """Context manager for a schema cache.

        Example usage::

            with catalog.cache() as cacheid:
                schema = JSONSchema(..., cacheid=cacheid)

        The cache and its contents are popped from the catalog
        upon exiting the ``with`` block.
        """
        if cacheid is None:
            cacheid = uuid.uuid4()

        if cacheid in self._schema_cache:
            raise CatalogError("cache identifier is already in use")

        try:
            yield cacheid
        finally:
            self._schema_cache.pop(cacheid, None)
